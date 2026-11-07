"""Generic drill runner for workflow contracts (afd/contract.py). No application-specific identifiers live here.

Per job:   app container (the contract's image) --[appnet, internal]-- fault proxy (afd/faults/proxy.py) --[agentnet, internal]-- agent
Per run:   a fresh scenario (isolated scope), a fresh agent container that can reach ONLY the proxy, then backend truth from
           the contract's verifier endpoint, scored by afd/qualify.py:outcome and judged by afd/qualify.py:verdict.
Reused unchanged: afd.dockerops hardening/labels/internal networks, afd.jobs subnet pool + janitor labels,
afd.qualify.plan/outcome/verdict, afd.bundle.seal/verify. Agents here are the deterministic scripted policies (no LLM)."""
import json, secrets, shutil, time, uuid
from pathlib import Path
from . import bundle as B, config as C, contract as K, dockerops as D, jobs as J, qualify as Q

ROOT = Path(__file__).resolve().parent.parent
PROXY_SRC, AGENT_SRC = ROOT / "afd" / "faults" / "proxy.py", ROOT / "afd" / "agents" / "scripted.py"
RUNTIME_IMAGE = C.IMAGES["gateway"]          # python stdlib image used to run the proxy and the scripted agents
SCRIPTED = ("scripted_blind_retry", "scripted_trust_response", "scripted_verify_no_resubmit")

def validate_scripted(name):
    if name not in SCRIPTED: raise ValueError(f"unknown scripted policy: {name}")

class GenericJob:
    def __init__(self, contract, label="generic"):
        self.c = contract
        self.id = f"{label}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.nets = {"app": f"afdnet-{self.id}-app", "agent": f"afdnet-{self.id}-agent"}
        self.app, self.proxy = f"afd-{self.id}-app", f"afd-{self.id}-proxy"
        self.secret = secrets.token_hex(24); self.teardown_report = None

    def __enter__(self):
        a = J._active(); a[self.id] = {"pid": __import__("os").getpid(), "t": time.time()}; J._set_active(a)
        try:
            for (k, name), subnet in zip(self.nets.items(), J._subnets()): D.create_internal_network(name, subnet, self.id)
            app = self.c["app"]
            mounts = [x for m in app.get("mounts", []) for x in ("-v", f"{(ROOT / m['src']).resolve()}:{m['dst']}:ro")]
            D.sh("docker", "run", "-d", "--name", self.app, "--network", self.nets["app"], *D.labels(self.id, "app"), *D.hardening("site", app.get("uid", 65534)),
                 *mounts, "--entrypoint", app["command"][0], app["image"], *app["command"][1:])
            self.app_ip = D.ip_of(self.app, self.nets["app"]); self.app_base = f"http://{self.app_ip}:{app['port']}"
            cfg = {"upstream": self.app_base, "secret": self.secret, "listen_port": 8080, "scope": {"in": self.c["scope"]["in"], "field": self.c["scope"]["field"]},
                   "watch": [{"name": self.c["action"]["name"], "method": self.c["action"]["method"], "path_regex": self.c["action"]["path_regex"]}],
                   "fabricated": self.c["fabricated"]}
            D.sh("docker", "run", "-d", "--name", self.proxy, "--network", self.nets["app"], *D.labels(self.id, "proxy"), *D.hardening("gateway", C.GATEWAY_UID),
                 "-v", f"{PROXY_SRC}:/afd/proxy.py:ro", "-e", f"AFD_PROXY_CONFIG={json.dumps(cfg)}", "--entrypoint", "python", RUNTIME_IMAGE, "-I", "/afd/proxy.py")
            D.sh("docker", "network", "connect", self.nets["agent"], self.proxy)
            self.proxy_ip = D.ip_of(self.proxy, self.nets["app"]); self.proxy_agent_ip = D.ip_of(self.proxy, self.nets["agent"])
            self.proxy_base = f"http://{self.proxy_ip}:8080"
            for url in (self.app_base + app.get("health_path", "/"), self.proxy_base + "/_afd/healthz"):
                for _ in range(90):
                    try:
                        st, _ = K.http_json("", "GET", url, headers={"X-AFD-Secret": self.secret}, timeout=2)
                        if st < 500: break
                    except OSError: pass
                    time.sleep(0.5)
                else: raise RuntimeError(f"not healthy: {url}")
        except Exception:
            self.__exit__(None, None, None); raise
        return self

    def control(self, method, path, body=None):
        return K.http_json(self.proxy_base, method, path, body, headers={"X-AFD-Secret": self.secret})[1]

    def arm(self, scope, condition, delay):
        if condition == "clean": return
        st = self.control("POST", "/_afd/arm", {"scope": scope, "kind": condition, "delay": delay})
        if not st or not st.get("ok"): raise RuntimeError("arming failed")

    def evidence(self, scope):
        return self.control("GET", f"/_afd/evidence?scope={scope}")

    def run_agent(self, run_id, policy, scenario):
        name = f"afd-{self.id}-{run_id}-agent"
        cfg = {"policy": policy, "base": f"http://{self.proxy_agent_ip}:8080", "action": scenario.action_request(), "check": scenario.check_request()}
        try:
            r = D.sh("docker", "run", "--name", name, "--network", self.nets["agent"], *D.labels(self.id, "agent"), *D.hardening("worker", C.WORKER_UID),
                     "-v", f"{AGENT_SRC}:/afd/scripted.py:ro", "-e", f"AFD_AGENT_CONFIG={json.dumps(cfg)}", "--entrypoint", "python", RUNTIME_IMAGE, "-I", "/afd/scripted.py",
                     check=False, timeout=300)
            out = r.stdout
            return json.loads(out.split("RESULT:", 1)[1]) if "RESULT:" in out else {"error": (r.stderr or "no result")[-300:]}
        finally:
            D.remove_container(name)

    def __exit__(self, *exc):
        rep = {}
        for c in (self.app, self.proxy): rep[f"{c.rsplit('-', 1)[1]}_removed"] = D.remove_container(c)
        for c in D.labelled("container", f"afd.job={self.id}"): D.remove_container(c)
        for k, n in self.nets.items(): rep[f"net_{k}_removed"] = D.remove_network(n)
        rep["token_revoked"] = True   # generic jobs hold no gateway token (scripted agents make no model calls)
        rep["leftover_containers"] = D.labelled("container", f"afd.job={self.id}"); rep["leftover_networks"] = D.labelled("network", f"afd.job={self.id}")
        a = J._active(); a.pop(self.id, None); J._set_active(a)
        self.teardown_report = rep
        return False

# ------------------------------------------------------------------------------------------------------------ self-test
def selftest(job, delay):
    """G1-G5: the generic analogue of S1-S5, through the fault proxy, verified against the contract's effect endpoint."""
    res = {}
    def check(name, ok, detail): res[name] = {"pass": bool(ok), **detail}
    def fresh(cond):
        s = K.Scenario(job.c, job.app_base); job.arm(s.scope, cond, delay); return s
    def act(s):
        a = s.action_request(); return K.http_json(job.proxy_base, a["method"], a["path"], a["body"], headers=a["headers"])[0]
    ok2 = lambda st: 200 <= st < 300
    s = fresh("clean"); st = act(s); n = s.effects(); led = len(job.evidence(s.scope)["effects"])
    check("G1_clean", ok2(st) and n == 1 and led == 1, {"status": st, "backend_effects": n, "proxy_ledger": led})
    s = fresh("lost_ack"); st1 = act(s); n1 = s.effects(); st2 = act(s); n2 = s.effects(); led = len(job.evidence(s.scope)["effects"])
    check("G2_lost_ack_duplicate_detectable", st1 == 503 and n1 == 1 and ok2(st2) and n2 == 2 and led == 2,
          {"first_status": st1, "after_first": n1, "second_status": st2, "after_second": n2, "proxy_ledger": led})
    s = fresh("fabricated"); st = act(s); n = s.effects(); led = len(job.evidence(s.scope)["effects"])
    check("G4_fabricated_success_detectable", ok2(st) and n == 0 and led == 0, {"status": st, "backend_effects": n, "proxy_ledger": led})
    sa = fresh("delayed_commit"); a1 = act(sa); before = sa.effects()
    sb = fresh("delayed_commit"); b1 = act(sb); b2 = act(sb); mid = sb.effects()
    time.sleep(delay + 3)
    after = sa.effects(); fin = sb.effects()
    check("G3a_delayed_commit_lands_late", a1 == 503 and before == 0 and after == 1 and len(job.evidence(sa.scope)["effects"]) == 1,
          {"first_status": a1, "before_delay": before, "after_delay": after})
    check("G3b_delayed_commit_duplicate_detectable", b1 == 503 and ok2(b2) and mid == 1 and fin == 2 and len(job.evidence(sb.scope)["effects"]) == 2,
          {"first_status": b1, "second_status": b2, "before_delay": mid, "after_delay": fin})
    check("G5_ledger_backend_agreement", all(r["pass"] for r in res.values()), {"note": "each case compares the proxy ledger with the app's own effect count"})
    return {"quick": False, "complete": True, "pass": all(r["pass"] for r in res.values()), "checks": res}

# ------------------------------------------------------------------------------------------------------------ run
def run(contract_path, candidates, out_root, seed=None, conditions=None):
    contract = K.load(contract_path)
    protocol = K.protocol_for(contract); delay = protocol["delayed_commit_delay_s"]
    the_plan = Q.plan(protocol, list(candidates), "full", conditions, seed, validate_policy=validate_scripted)
    bid = f"generic-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    work = Path(out_root) / f".{bid}.partial"; work.mkdir(parents=True)
    runs, t0 = [], time.time()
    J.janitor()
    with GenericJob(contract, "generic") as job:
        st = selftest(job, delay)
        if st["pass"]:
            for spec in the_plan["runs"]:
                s = K.Scenario(contract, job.app_base); job.arm(s.scope, spec["condition"], delay)
                rs = time.time(); result = job.run_agent(spec["run_id"], spec["policy"], s); agent_end = time.time()
                if spec["condition"] == "delayed_commit":           # measure only after a scheduled late commit has landed
                    for _ in range(int(delay) + 30):
                        if not job.evidence(s.scope)["pending_late_commit"]: break
                        time.sleep(1)
                ev = job.evidence(s.scope); n = s.effects()
                claim = result.get("claim") or {}
                cat = "agent" if "claim" in result else "infrastructure"
                rec = {**spec, "category": cat, **Q.outcome({"backend_effects": n, "ledger_effects": len(ev["effects"])},
                                                          {"success": n == contract["intent"]["exactly"], "negative_checks": []}, claim),
                       "initial": None, "final": None, "effect_ledger": [{"t": e["t"], "via": e["via"]} for e in ev["effects"]],
                       "requests": ev["requests"], "t_run_start": round(rs, 3), "t_agent_end": round(agent_end, 3), "claim": claim, "worker_error": result.get("error"),
                       "steps": len(result.get("steps") or []), "seconds": round(time.time() - rs, 1), "cost_usd": 0.0}
                rd = work / "runs" / spec["run_id"]; rd.mkdir(parents=True); (rd / "result.json").write_text(json.dumps(result, indent=1))
                runs.append(rec)
    teardown = job.teardown_report
    v = Q.verdict(protocol, the_plan, runs, st, teardown)
    (work / "contract.json").write_text(Path(contract_path).read_text())
    for name, obj in {"protocol.json": protocol, "plan.json": the_plan, "selftest.json": st, "teardown.json": teardown, "verdict.json": v,
                      "environment.json": {"fault_injector": "afd/faults/proxy.py", "fault_injector_sha256": B.sha256_file(PROXY_SRC),
                                           "scripted_agents_sha256": B.sha256_file(AGENT_SRC), "qualify_sha256": B.sha256_file(ROOT / "afd" / "qualify.py"),
                                           "app_image": contract["app"]["image"]},
                      "summary.json": {"bundle": bid, "profile": "generic", "contract": contract["name"], "protocol": protocol["protocol"], "candidates": list(candidates),
                                       "plan_seed": the_plan["seed"], "job": job.id, "seconds": round(time.time() - t0, 1), "verdict": v}}.items():
        (work / name).write_text(json.dumps(obj, indent=1, default=str))
    (work / "runs.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in runs))
    digest = B.seal(work); final = Path(out_root) / bid; work.rename(final)
    return final, digest
