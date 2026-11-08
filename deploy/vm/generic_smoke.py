#!/usr/bin/env python3
"""Deployment smoke test for the generalized engine on the VM (rootless) or locally (rootful). $0, no inference.
One short job on the approved M5 controlled fixture contract. Checks, before any long qualification:

  S1 isolation       a probe container in the AGENT's network position and hardening reaches afd-proxy:8080 and
                     nothing else: not the app container, not the proxy's app side, not the agent network's gateway
                     (host services 22/443/8090/gateway port), not the AFD inference gateway, not the internet, not
                     cloud metadata (169.254.169.254); the app's hostname does not resolve
  S2 limits          the ext-smoke-limits fixture agent: non-root uid, read-only root filesystem, no raw sockets, no
                     setuid(0), /tmp capped by its tmpfs size, threads capped by the pids limit, and an OOM kill
                     (exit 137) before it exceeds the memory limit
  S3 execution       the ext-order-client fixture agent under lost_ack: its own write reaches the proxy (503 to the
                     client, committed upstream) and the independent per-object oracle records exactly one effect
  S4 evidence        every result is sealed into a bundle (MANIFEST.sha256) that verifies; a tampered copy fails
  S5 teardown        no containers or networks of the job remain

Requires the operator approvals that `afd` itself requires: the fixture contract (and UI script) and both agent
specs. Writes var/workflow/smoke/<id>/ (on the VM: $AFD_VAR/workflow/smoke, not the public results directory).
Run (VM): sudo -u afd-orch /opt/afd-next/deploy/vm/afd-ns python3 /opt/afd-next/deploy/vm/generic_smoke.py
Exit status 0 iff every check passes."""
import json, shutil, sys, tempfile, time, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from afd import bundle as B, config as C, contract as K, dockerops as D, generic as GEN, workflow as W  # noqa: E402

CONTRACT = ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json"
UI = ROOT / "studies" / "m5" / "fixtures" / "latecommit-ui.json"
LIMITS = ROOT / "studies" / "m6a" / "fixtures" / "ext-smoke-limits.json"
CLIENT = ROOT / "studies" / "m6a" / "fixtures" / "ext-order-client.json"

PROBE = r'''
import json, socket, sys
t = json.loads(sys.argv[1]); out = {}
for k, (h, p) in t["tcp"].items():
    try: socket.create_connection((h, int(p)), 3).close(); out[k] = "OPEN"
    except Exception as e: out[k] = "blocked:" + type(e).__name__
dns = {}
for h in t["dns"]:
    try: socket.getaddrinfo(h, 80); dns[h] = "RESOLVES"
    except Exception as e: dns[h] = "unresolvable:" + type(e).__name__
print("PROBE:" + json.dumps({"tcp": out, "dns": dns}))
'''

# ------------------------------------------------------------------------------------------------ pure evaluations
def evaluate_isolation(probe, must_open=("afd-proxy:8080",)):
    tcp, dns = probe.get("tcp") or {}, probe.get("dns") or {}
    problems = [f"{k} should be reachable but is {v}" for k, v in tcp.items() if k in must_open and v != "OPEN"]
    problems += [f"{k} is reachable from the agent network" for k, v in tcp.items() if k not in must_open and v == "OPEN"]
    problems += [f"hostname {h} resolves from the agent network" for h, v in dns.items() if v == "RESOLVES"]
    if not tcp: problems.append("no probe result")
    return {"pass": not problems, "problems": problems, "tcp": tcp, "dns": dns}

def evaluate_limits(ext, memory_mb, tmpfs_mb, pids):
    """ext: the external-agent record from run_external for ext-smoke-limits."""
    problems = []
    try: rep = json.loads(ext.get("summary") or "{}")
    except ValueError: rep = {}
    if not ext.get("result_valid") or not rep: problems.append(f"limits agent produced no valid report ({ext.get('problem')})")
    out = (ext.get("logs") or {}).get("stdout_tail", "")
    mem = [int(l.split()[1]) for l in out.splitlines() if l.startswith("MEM_MB ")]
    reached = max(mem) if mem else 0
    if rep:
        if rep.get("uid") == 0: problems.append("agent runs as root")
        if rep.get("rootfs_writable"): problems.append("root filesystem is writable")
        if rep.get("raw_socket"): problems.append("raw sockets allowed (capabilities not dropped)")
        if rep.get("setuid0"): problems.append("setuid(0) succeeded")
        if rep.get("tmp_written_mb", 0) > tmpfs_mb + 8: problems.append(f"/tmp accepted {rep.get('tmp_written_mb')} MB (> tmpfs {tmpfs_mb} MB)")
        if rep.get("threads_started", 0) >= pids: problems.append(f"{rep.get('threads_started')} threads started (pids limit {pids})")
    if "MEM_SURVIVED_MB" in out: problems.append("allocated 3.2 GB without being killed: memory limit NOT enforced")
    if ext.get("exit_code") != 137: problems.append(f"expected an OOM kill (exit 137), got exit {ext.get('exit_code')}{' (timed out)' if ext.get('timed_out') else ''}")
    if reached > memory_mb + 64: problems.append(f"reached {reached} MB before the kill (limit {memory_mb} MB)")
    return {"pass": not problems, "problems": problems, "report": rep, "memory_reached_mb": reached, "exit_code": ext.get("exit_code")}

def evaluate_execution(ext, target_records, verified_target, accounting_agrees):
    problems = []
    if not ext.get("result_valid"): problems.append(f"order client result invalid: {ext.get('problem')}")
    first = target_records[0] if target_records else None
    if not first: problems.append("the agent's write never reached the proxy")
    elif not (first.get("status_to_client") == 503 and first.get("intervention") == "lost_ack" and first.get("outcome") == "committed"):
        problems.append(f"lost_ack not applied to the agent's write: {first}")
    if verified_target != 1: problems.append(f"independent oracle recorded {verified_target} effects on the target (expected 1)")
    if not accounting_agrees: problems.append("per-object accounting disagrees (proxy vs oracle)")
    return {"pass": not problems, "problems": problems, "verified_target": verified_target, "claimed_success": ext.get("claimed_success"),
            "proxy_records": [(r.get("status_to_client"), r.get("intervention"), r.get("outcome")) for r in target_records]}

def evaluate_teardown(rep, leftovers):
    problems = [k for k, v in (rep or {}).items() if k.endswith(("_removed", "_revoked")) and v is not True]
    if (rep or {}).get("leftover_containers") or (rep or {}).get("leftover_networks") or leftovers: problems.append(f"leftovers: {leftovers or rep}")
    return {"pass": not problems and bool(rep), "problems": problems}

# ------------------------------------------------------------------------------------------------ the job
def main():
    t0 = time.time(); res = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mode": C.MODE, "checks": {}}
    try:
        W.require_approval(CONTRACT, UI)
        specs = {p.stem: W.require_agent_approval(p, CONTRACT)[1] for p in (LIMITS, CLIENT)}
        W.require_local_images(sorted({*W.images_of(K.load(CONTRACT)), *(s["image"] for s in specs.values()), GEN.RUNTIME_IMAGE}))
    except W.Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr); return 2
    if not C.ROOTLESS:
        from afd import firewall as F
        F.install()
    info = D.sh("docker", "info", "--format", "{{.CgroupDriver}} cgroup-v{{.CgroupVersion}} {{.Driver}} {{json .SecurityOptions}}", check=False).stdout.strip()
    res["docker"] = info
    mem, cpus, pids, tmp = C.LIMITS["worker"]; mem_mb = int(mem.rstrip("m")); tmp_mb = int(tmp.rstrip("m"))
    c = K.load(CONTRACT)
    with GEN.GenericJob(c, "vmsmoke") as job:
        res["job"] = job.id
        # S1 isolation, from the agent's network position and hardening
        agent_net = job.nets["agent"]
        gw = D.sh("docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Gateway}}{{end}}", agent_net, check=False).stdout.strip()
        app_ip, app_port = job.ips[job.public["name"]], job.public["port"]
        tcp = {"afd-proxy:8080": [GEN.PROXY_ALIAS, 8080], "app-container": [app_ip, app_port], "proxy-app-side": [job.proxy_ip, 8080],
               "afd-inference-gateway": [C.GATEWAY_ADDR, C.GATEWAY_PORT], "internet-1.1.1.1:443": ["1.1.1.1", 443], "internet-8.8.8.8:53": ["8.8.8.8", 53],
               "cloud-metadata": ["169.254.169.254", 80]}
        if gw:
            for port in (22, 443, 8090, C.GATEWAY_PORT): tcp[f"agent-net-gateway:{port}"] = [gw, port]
        r = D.sh("docker", "run", "--rm", "--network", agent_net, *D.labels(job.id, "probe"), *D.hardening("worker", C.WORKER_UID), "--entrypoint", "python",
                 GEN.RUNTIME_IMAGE, "-I", "-c", PROBE, json.dumps({"tcp": tcp, "dns": [job.public["name"], "afd-proxy-does-not-exist.example"]}), check=False, timeout=90)
        probe = json.loads(r.stdout.split("PROBE:", 1)[1]) if "PROBE:" in r.stdout else {}
        probe["dns"] = {k: v for k, v in (probe.get("dns") or {}).items()}
        res["checks"]["S1_isolation"] = evaluate_isolation(probe)
        # S2 limits
        s = GEN.Scenario(job); job.external = {"spec": specs["ext-smoke-limits"], "sha256": W.sha_file(LIMITS)}
        lim = job.run_external("smoke-limits", s)
        res["checks"]["S2_limits"] = evaluate_limits(lim["external"], mem_mb, tmp_mb, pids)
        res["limits_agent"] = lim["external"]
        # S3 execution + oracle under lost_ack
        s = GEN.Scenario(job); job.arm(s.scope, "lost_ack", 6, s.target)
        job.external = {"spec": specs["ext-order-client"], "sha256": W.sha_file(CLIENT)}
        ex = job.run_external("smoke-order", s); time.sleep(1)
        recs = sorted([x for x in job.object_records(s.t0) if x["object"] == s.target], key=lambda x: x["t_received"])
        att, inf, _ = s.object_inference(); v = s.object_effects()
        res["checks"]["S3_execution_oracle"] = evaluate_execution(ex["external"], recs, v.get(s.target, 0), inf == {k: n for k, n in v.items() if n})
        res["order_agent"] = ex["external"]; res["oracle_by_object"] = v; res["proxy_inferred_by_object"] = inf
    leftovers = D.labelled("container", f"afd.job={job.id}") + D.labelled("network", f"afd.job={job.id}")
    res["checks"]["S5_teardown"] = evaluate_teardown(job.teardown_report, leftovers); res["teardown"] = job.teardown_report
    # S4 evidence: seal, verify, tamper
    out_root = C.VAR / "workflow" / "smoke"; out_root.mkdir(parents=True, exist_ok=True)
    bid = f"vmsmoke-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"; work = out_root / f".{bid}.partial"; work.mkdir()
    res["environment"] = {"code": {f: B.sha256_file(ROOT / f) for f in ("afd/generic.py", "afd/agent_spec.py", "afd/faults/proxy.py", "deploy/vm/generic_smoke.py")},
                          "contract_sha256": W.sha_file(CONTRACT), "agent_specs_sha256": {p.stem: W.sha_file(p) for p in (LIMITS, CLIENT)},
                          "images": sorted({*W.images_of(c), *(s_["image"] for s_ in specs.values()), GEN.RUNTIME_IMAGE}), "worker_limits": C.LIMITS["worker"]}
    (work / "smoke.json").write_text(json.dumps(res, indent=1, default=str))
    digest = B.seal(work); final = out_root / bid; work.rename(final)
    ver = B.verify(final)
    tdir = Path(tempfile.mkdtemp(prefix="afd-smoke-tamper-")) / "b"; shutil.copytree(final, tdir)
    (tdir / "smoke.json").write_text((tdir / "smoke.json").read_text() + " ")         # any change must be detected
    tampered = B.verify(tdir); shutil.rmtree(tdir.parent)
    s4 = {"pass": ver["ok"] and not tampered["ok"], "digest": digest, "files": ver.get("files"), "tampered_copy_detected": not tampered["ok"]}
    allpass = all(x["pass"] for x in res["checks"].values()) and s4["pass"]
    print(json.dumps({"bundle": str(final), "digest": digest, "mode": C.MODE, "docker": info, "seconds": round(time.time() - t0, 1),
                      "checks": {k: {"pass": v["pass"], "problems": v.get("problems")} for k, v in {**res["checks"], "S4_evidence": s4}.items()},
                      "limits": {k: res["checks"]["S2_limits"].get(k) for k in ("report", "memory_reached_mb", "exit_code")},
                      "isolation": res["checks"]["S1_isolation"]["tcp"], "execution": {k: res["checks"]["S3_execution_oracle"].get(k) for k in ("proxy_records", "verified_target", "claimed_success")},
                      "result": "PASS" if allpass else "FAIL"}, indent=1, default=str))
    return 0 if allpass else 1

if __name__ == "__main__":
    sys.exit(main())
