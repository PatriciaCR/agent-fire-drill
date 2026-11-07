"""Rootless containment suite, ORCHESTRATOR side (no inference cost). Runs as afd-orch inside the rootless Docker namespaces
(started by tests/acceptance/rootless_suite.py via deploy/vm/afd-ns, AFD_MODE=rootless). Probes run inside containers with
exactly the hardening and networks of their role in a real job. This side cannot read the key: the worker's file/env scan
is written to $AFD_VAR/acceptance/worker_files.bin (0600) and compared with the key by the root-side driver.
Env from the driver: AFD_PROBE_HOST_IPS (host addresses, comma separated), AFD_PROBE_HOST_PORT (a root-owned listener).
Writes $AFD_VAR/acceptance/rootless_orch.json."""
import base64, json, os, signal, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from afd import config as C, dockerops as D, firewall as F, gateway as G, jobs as J  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import containment as K  # noqa: E402  (shared probes: WORKER_PROBE, run_probe_container)

assert C.ROOTLESS, "run with AFD_MODE=rootless (via deploy/vm/afd-ns)"
RESULTS, OK = {}, [True]
def check(name, cond, detail=None):
    RESULTS[name] = {"pass": bool(cond), "detail": detail}; OK[0] &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {json.dumps(detail, default=str)[:220] if detail is not None else ''}", flush=True)

HOST_IPS = [h for h in os.environ.get("AFD_PROBE_HOST_IPS", "").split(",") if h]
HOST_PORT = int(os.environ.get("AFD_PROBE_HOST_PORT", "18080"))
# Everything a worker might try outside its job besides the gateway port: other ports on the gateway address, the host's
# own addresses, slirp4netns' host-loopback and DNS addresses, cloud metadata, the internet.
EXTRA = [f"{C.GATEWAY_ADDR}:22", f"{C.GATEWAY_ADDR}:{HOST_PORT}", f"10.0.2.2:{HOST_PORT}", "10.0.2.2:22", "10.0.2.3:53", "169.254.169.254:80",
         "1.1.1.1:443", "8.8.8.8:53"] + [f"{h}:{HOST_PORT}" for h in HOST_IPS] + [f"{h}:22" for h in HOST_IPS]
EXTRA_PROBE = r'''
import json, os, socket
R = {}
for t in os.environ["TARGETS"].split(","):
    h, p = t.rsplit(":", 1)
    try: socket.create_connection((h, int(p)), timeout=3).close(); R[t] = "OPEN"
    except Exception as e: R[t] = "blocked:" + type(e).__name__
socks = []
for base in ("/run", "/var/run", "/tmp", "/home", "/root"):
    for root, dirs, files in os.walk(base):
        for f in files:
            try:
                if __import__("stat").S_ISSOCK(os.lstat(os.path.join(root, f)).st_mode): socks.append(os.path.join(root, f))
            except OSError: pass
R["_unix_sockets"] = socks
R["_gw_ok"] = None
import urllib.request
try: R["_gw_ok"] = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(os.environ["GW"] + "/healthz", timeout=5).status
except Exception as e: R["_gw_ok"] = "blocked:" + type(e).__name__
print("EXTRA:" + json.dumps(R))
'''

def probe_extra(job, nets):
    r, _ = K.run_probe_container(job, "worker", C.IMAGES["worker"], nets, ["python", "-c", EXTRA_PROBE],
                                 {"TARGETS": ",".join(EXTRA), "GW": G.url()}, C.WORKER_UID)
    return json.loads(r.stdout.split("EXTRA:", 1)[1]) if "EXTRA:" in r.stdout else {}

def cgroup_limits(container):
    out = D.sh("docker", "exec", container, "sh", "-c", "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.max /sys/fs/cgroup/cpu.max 2>&1", check=False).stdout.split("\n")
    return [x.strip() for x in out if x.strip()]

def expected(role):
    mem, cpus, pids, _ = C.LIMITS[role]
    n = int(mem[:-1]) * (1024 ** 3 if mem.endswith("g") else 1024 ** 2)
    return [str(n), str(pids), f"{int(float(cpus) * 100000)} 100000"]

def main():
    try: suite()
    except Exception as e:   # a crash is a failure, never a silent pass; results so far are still written
        import traceback; traceback.print_exc(); check("orchestrator_suite_completed", False, f"{type(e).__name__}: {e}")
    else: check("orchestrator_suite_completed", True)
    out = C.VAR / "acceptance" / "rootless_orch.json"; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pass": OK[0], "checks": RESULTS}, indent=1, default=str))
    print("ORCH-SIDE:", "PASS" if OK[0] else "FAIL"); sys.exit(0 if OK[0] else 1)

def suite():
    G.up()
    check("gateway_service_reachable_from_namespace", True, {"url": G.url()})
    F.ensure_netns(); check("netns_rules_installed", "AFD-NS" in F.show_netns(), F.show_netns().strip().splitlines()[:8])
    with J.Job(budget_usd=0.01, label="containment") as job:
        browser = f"afd-{job.id}-pb-browser"
        D.sh("docker", "run", "-d", "--name", browser, "--network", job.nets["site"], *D.labels(job.id, "browser"), *D.hardening("browser", C.BROWSER_UID), C.IMAGES["browser"])
        D.sh("docker", "network", "connect", job.nets["cdp"], browser); cdp_ip = D.ip_of(browser, job.nets["cdp"])
        check("browser_cdp_ready", True, {"seconds": J.wait_cdp(cdp_ip)})
        # per-role cgroup limits as seen inside running containers (site, browser)
        for role, cname in (("site", job.site), ("browser", browser)):
            got = cgroup_limits(cname)
            check(f"{role}_cgroup_limits_applied", got == expected(role), {"memory.max, pids.max, cpu.max": got, "expected": expected(role)})
        env = {"HOST_PORT": HOST_PORT, "HOST_BRIDGE_IPS": ",".join(HOST_IPS + ["10.0.2.2", C.GATEWAY_ADDR]), "SITE_IP": job.site_ip, "GW": G.url(),
               "CDP": f"http://{cdp_ip}:9222", "AFD_JOB_TOKEN": job.token}
        wnets = [job.nets["cdp"], job.nets["gw"]]
        r, state = K.run_probe_container(job, "worker", C.IMAGES["worker"], wnets, ["python", "-c", K.WORKER_PROBE], env, C.WORKER_UID)
        P = json.loads(r.stdout.split("PROBE:", 1)[1]) if "PROBE:" in r.stdout else {}
        blob = base64.b64decode(P.pop("_files_b64", "")) if P else b""
        acc = C.VAR / "acceptance"; acc.mkdir(parents=True, exist_ok=True)
        bf = acc / "worker_files.bin"; bf.write_bytes(blob); os.chmod(bf, 0o600)
        check("worker_scan_written_for_admin_key_comparison", bool(P) and len(blob) > 0, {"scanned_bytes": len(blob)})
        check("worker_no_docker_socket", P.get("docker_sock") == [] and P.get("secrets_dir") is False, {k: P.get(k) for k in ("docker_sock", "secrets_dir")})
        check("worker_no_internet", str(P.get("internet_1.1.1.1:80")).startswith("blocked"), P.get("internet_1.1.1.1:80"))
        check("worker_no_direct_inference", str(P.get("inference_direct")).startswith("blocked"), P.get("inference_direct"))
        check("worker_no_metadata", str(P.get("metadata")).startswith("blocked"), P.get("metadata"))
        check("worker_no_host_ports", all(str(v).startswith("blocked") for v in (P.get("host_ports") or {"x": "OPEN"}).values()), P.get("host_ports"))
        check("worker_cannot_reach_site_directly", str(P.get("site_direct")).startswith("blocked"), P.get("site_direct"))
        check("worker_reaches_gateway_and_cdp_only_as_allowed", P.get("gateway_health") == 200 and P.get("gateway_no_token") == 401 and P.get("gateway_with_token_bad_model") == 403 and P.get("cdp") == 200,
              {k: P.get(k) for k in ("gateway_health", "gateway_no_token", "gateway_with_token_bad_model", "cdp")})
        check("worker_identity_and_limits", P.get("uid") == C.WORKER_UID and P.get("CapEff") == "0000000000000000" and P.get("NoNewPrivs") == "1"
              and str(P.get("root_write")).startswith("blocked") and P.get("memory_max") == expected("worker")[0] and P.get("pids_max") == expected("worker")[1],
              {k: P.get(k) for k in ("uid", "CapEff", "NoNewPrivs", "root_write", "memory_max", "pids_max")})
        X = probe_extra(job, wnets)
        socks = X.pop("_unix_sockets", None); gw = X.pop("_gw_ok", None)
        check("worker_blocked_from_every_non_gateway_target", bool(X) and all(str(v).startswith("blocked") for v in X.values()), X)
        check("worker_no_unix_sockets_visible", socks == [], socks)
        # ---- the host boundary alone: remove the in-namespace rules and probe again (they are defence in depth only) ----
        F._ipt_ns("-F", "AFD-NS"); F._ipt_ns("-F", "AFD-NS-IN")
        try:
            X2 = probe_extra(job, wnets); X2.pop("_unix_sockets", None); gw2 = X2.pop("_gw_ok", None)
            check("host_boundary_holds_without_namespace_rules", bool(X2) and all(str(v).startswith("blocked") for v in X2.values()) and gw2 == 200,
                  {"targets": X2, "gateway_still_reachable": gw2})
        finally:
            F.ensure_netns()
        # ---- resource-limit enforcement (rootless: requires delegated cgroup v2 controllers) ----
        r, st = K.run_probe_container(job, "worker", C.IMAGES["worker"], [job.nets["cdp"]], ["python", "-c", "b = bytearray(3 * 1024**3)"], {}, C.WORKER_UID)
        check("worker_memory_limit_enforced", st.get("OOMKilled") is True or st.get("ExitCode") == 137, {"OOMKilled": st.get("OOMKilled"), "exit": st.get("ExitCode")})
        fork = "import os, time\nn=0\ntry:\n    for _ in range(3000):\n        if os.fork()==0: time.sleep(5); os._exit(0)\n        n+=1\n    print('FORKS', n)\nexcept OSError: print('FORKS', n)"
        r, st = K.run_probe_container(job, "worker", C.IMAGES["worker"], [job.nets["cdp"]], ["python", "-c", fork], {}, C.WORKER_UID)
        nforks = int(r.stdout.split("FORKS")[-1].strip() or 0) if "FORKS" in r.stdout else None
        check("worker_pids_limit_enforced", nforks is not None and nforks < C.LIMITS["worker"][2], {"forks_before_limit": nforks, "pids_limit": C.LIMITS["worker"][2]})
        cpu = "import os,time\nt=time.time(); c=time.process_time()\nwhile time.time()-t<4: pass\nos.write(1, ('CPU %.2f\\n' % ((time.process_time()-c)/(time.time()-t))).encode())"
        par = "import os,sys\nfor _ in range(3):\n  if os.fork()==0:\n    exec(sys.argv[1]); os._exit(0)\nexec(sys.argv[1])\nos.wait();os.wait();os.wait()"
        r, st = K.run_probe_container(job, "worker", C.IMAGES["worker"], [job.nets["cdp"]], ["python", "-c", par, cpu], {}, C.WORKER_UID)
        shares = [float(x.split()[1]) for x in r.stdout.splitlines() if x.startswith("CPU")]
        check("worker_cpu_limit_enforced", len(shares) == 4 and sum(shares) <= float(C.LIMITS["worker"][1]) * 1.15, {"per_process_cpu_share": shares, "cpus_limit": C.LIMITS["worker"][1]})
        # ---- browser ----
        bprobe = ("for t in 1.1.1.1:80 169.254.169.254:80 " + " ".join(f"{h}:{HOST_PORT}" for h in HOST_IPS) + f" {C.GATEWAY_ADDR}:{C.GATEWAY_PORT} 10.0.2.2:{HOST_PORT} {job.site_ip}:8080; do "
                  "if socat -u OPEN:/dev/null TCP:$t,connect-timeout=3 2>/dev/null; then echo \"$t OPEN\"; else echo \"$t blocked\"; fi; done; "
                  "id -u; grep CapEff /proc/self/status | cut -f2; env | cut -d= -f1 | sort | tr '\\n' ' '")
        r, _ = K.run_probe_container(job, "browser", C.IMAGES["browser"], [job.nets["site"], job.nets["cdp"]], ["-c", bprobe], {}, C.BROWSER_UID, entry="sh")
        lines = r.stdout.strip().splitlines()
        res = dict(l.split(" ", 1) for l in lines if " " in l and l.split(" ")[0].count(":") == 1 and l.split(" ")[0].split(":")[0].count(".") == 3)
        check("browser_reaches_site_only", res.get(f"{job.site_ip}:8080") == "OPEN" and len(res) >= 4 and all(v == "blocked" for k, v in res.items() if k != f"{job.site_ip}:8080"), res)
        check("browser_identity_and_env", lines[-3:-2] == [str(C.BROWSER_UID)] and lines[-2] == "0000000000000000" and not any(s in lines[-1] for s in ("KEY", "TOKEN", "SECRET")),
              {"uid": lines[-3:-2], "CapEff": lines[-2:-1], "env": lines[-1][:200]})
        D.remove_container(browser)
        old_token = job.token
    rep = job.teardown_report
    check("teardown_removes_everything", all(v is True for k, v in rep.items() if k.endswith(("_removed", "_revoked"))) and not rep["leftover_containers"] and not rep["leftover_networks"], rep)
    try:
        J._NOPROXY.open(J.urllib.request.Request(G.url() + "/v1/chat/completions", method="POST", data=b'{"model":"x"}',
                        headers={"Authorization": f"Bearer {old_token}", "Content-Type": "application/json"}), timeout=5); code = 200
    except J.urllib.error.HTTPError as e: code = e.code
    except Exception as e: code = f"unreachable:{type(e).__name__}"
    check("revoked_token_rejected_after_job", code == 401, code)
    p = subprocess.Popen([sys.executable, "-I", "-c", "import sys,time; sys.path.insert(0, %r); from afd import jobs as J\nj=J.Job(budget_usd=0.01,label='crash').__enter__()\nprint('UP', j.id, flush=True)\ntime.sleep(600)" % str(C.ROOT)],
                         stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline().strip(); crash_job = line.split()[1] if line.startswith("UP") else None
    os.kill(p.pid, signal.SIGKILL); p.wait()
    before = {"containers": D.labelled("container", f"afd.job={crash_job}"), "networks": D.labelled("network", f"afd.job={crash_job}")}
    removed = J.janitor()
    after = {"containers": D.labelled("container", f"afd.job={crash_job}"), "networks": D.labelled("network", f"afd.job={crash_job}")}
    check("janitor_cleans_after_orchestrator_sigkill", bool(before["containers"]) and not after["containers"] and not after["networks"] and len(removed["tokens"]) >= 1,
          {"left_by_crash": before, "after_janitor": after, "tokens_revoked": len(removed["tokens"])})

if __name__ == "__main__":
    main()
