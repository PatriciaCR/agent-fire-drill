"""Generic drill runner for workflow contracts (afd/contract.py, v0 normalised to v1). No application-specific identifiers.

Per job:   services (the contract's containers) --[appnet, internal]-- fault proxy --[agentnet, internal]-- agent
           Services reach each other by service name; the agent reaches the app only as http://afd-proxy:8080.
Per run:   per-run setup (fresh scope), a fresh agent container, then backend truth from the contract's oracle.
Every run reports three distinct effect quantities (compatibility-study amendment AM-1):
  attempts         watched requests the proxy saw
  proxy_inferred   attempts whose upstream status matched the contract's success classification (an INFERENCE)
  verified         the independent oracle's count, which is AUTHORITATIVE; all scoring uses it
Reused unchanged: afd.dockerops hardening/labels/internal networks, afd.jobs subnet pool + janitor labels,
afd.qualify.plan/outcome/verdict, afd.bundle.seal/verify.
Contracts with an "objects" section (afd-workflow-contract/1.1) are scored under protocol contract-v1 by
afd/scoring_objects.py: effects are counted PER OBJECT from the independent per-object oracle (target, duplicate target,
other objects), the proxy's per-object records are the inference they are reconciled against, and write attempts
are never counted as effects. Contracts without it keep the contract-v0 path below, unchanged."""
import json, re, secrets, socket, time, uuid
from pathlib import Path
from . import bundle as B, config as C, contract as K, dockerops as D, jobs as J, qualify as Q, scoring_objects as SO

ROOT = Path(__file__).resolve().parent.parent
PROXY_SRC, AGENT_SRC = ROOT / "afd" / "faults" / "proxy.py", ROOT / "afd" / "agents" / "scripted.py"
RUNTIME_IMAGE = C.IMAGES["gateway"]          # python stdlib image used to run the proxy and the scripted agents
SCRIPTED = ("scripted_blind_retry", "scripted_trust_response", "scripted_verify_no_resubmit", "scripted_wrong_target")
PROXY_ALIAS = "afd-proxy"

def validate_scripted(name):
    if name not in SCRIPTED: raise ValueError(f"unknown scripted policy: {name}")

class GenericJob:
    def __init__(self, contract, label="generic"):
        self.c = contract
        self.id = f"{label}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.nets = {"app": f"afdnet-{self.id}-app", "agent": f"afdnet-{self.id}-agent"}
        self.proxy = f"afd-{self.id}-proxy"
        self.containers = {s["name"]: f"afd-{self.id}-{s['name']}" for s in contract["services"]}
        self.secret = secrets.token_hex(24); self.teardown_report = None
        self.vars = {k: (v["value"] if isinstance(v, dict) else v) for k, v in (contract.get("vars") or {}).items()}
        self.secret_vars = {k for k, v in (contract.get("vars") or {}).items() if isinstance(v, dict) and v.get("secret")}
        self.vars["public_url"] = f"http://{PROXY_ALIAS}:8080"
        self.public = next(s for s in contract["services"] if s.get("public"))
        self.captured_names = []

    # ---- lifecycle ----
    def __enter__(self):
        a = J._active(); a[self.id] = {"pid": __import__("os").getpid(), "t": time.time()}; J._set_active(a)
        try:
            for (k, name), subnet in zip(self.nets.items(), J._subnets()): D.create_internal_network(name, subnet, self.id)
            self.ips = {}
            for s in self.c["services"]:
                self._start_service(s)
            pub = self.public
            cfg = {"upstream": f"http://{pub['name']}:{pub['port']}", "secret": self.secret, "listen_port": 8080, "scope": self.c["scope"],
                   "watch": [{"name": self.c["action"]["name"], "method": self.c["action"]["method"], "path_regex": self.c["action"]["path_regex"]}],
                   "success_status": self.c["action"].get("success_status"), "fabricated": K.render(self.c["fabricated"], **self.vars)}
            if "objects" in self.c: cfg["objects"] = {"id": self.c["objects"]["id"], "routes": self.c["objects"].get("routes", [])}
            D.sh("docker", "run", "-d", "--name", self.proxy, "--network", self.nets["app"], "--network-alias", PROXY_ALIAS, *D.labels(self.id, "proxy"),
                 *D.hardening("gateway", C.GATEWAY_UID), "-v", f"{PROXY_SRC}:/afd/proxy.py:ro", "-e", f"AFD_PROXY_CONFIG={json.dumps(cfg)}",
                 "--entrypoint", "python", RUNTIME_IMAGE, "-I", "/afd/proxy.py")
            D.sh("docker", "network", "connect", "--alias", PROXY_ALIAS, self.nets["agent"], self.proxy)
            self.proxy_ip = D.ip_of(self.proxy, self.nets["app"]); self.proxy_base = f"http://{self.proxy_ip}:8080"
            self._wait_http(self.proxy_base + "/_afd/healthz", {"X-AFD-Secret": self.secret}, ok=lambda st: st == 200)
            for step in self.c.get("setup_once", []): self.vars.update(self.run_step(step, self.vars))
        except Exception:
            self.__exit__(None, None, None); raise
        return self

    def _start_service(self, s):
        name = self.containers[s["name"]]
        env = [x for k, v in K.render(s.get("env") or {}, **self.vars).items() for x in ("-e", f"{k}={v}")]
        uid = s.get("uid", 65534)    # writable paths are owned by the service's own (non-root) uid, mode 0755: no shared world-writable dirs
        tmp = [x for p in s.get("writable", []) for x in ("--tmpfs", f"{p}:rw,uid={uid},gid={uid},mode=0755,size=512m")]
        sysctl = [x for k, v in (s.get("sysctls") or {}).items() for x in ("--sysctl", f"{k}={v}")]
        mounts = [x for m in s.get("mounts", []) for x in ("-v", f"{(ROOT / m['src']).resolve()}:{m['dst']}:ro")]
        ent = ["--entrypoint", s["entrypoint"][0]] if s.get("entrypoint") else []
        args = (s.get("entrypoint") or [])[1:] + K.render(s.get("args") or [], **self.vars)
        D.sh("docker", "run", "-d", "--name", name, "--network", self.nets["app"], "--network-alias", s["name"], *D.labels(self.id, f"svc-{s['name']}"),
             *D.hardening("site", uid), *tmp, *sysctl, *env, *mounts, *ent, s["image"], *args)
        ip = D.ip_of(name, self.nets["app"]); self.ips[s["name"]] = ip
        h = s.get("health") or {}
        if "tcp" in h:
            for _ in range(int(h.get("timeout_s", 120)) * 2):
                try: socket.create_connection((ip, int(h["tcp"])), 1).close(); break
                except OSError: time.sleep(0.5)
            else: raise RuntimeError(f"service {s['name']} not reachable on tcp {h['tcp']}")
        else:
            self._wait_http(f"http://{ip}:{s['port']}{h.get('path', '/')}", {}, ok=lambda st: st in (h.get("expect") or range(200, 500)), timeout=int(h.get("timeout_s", 120)))
        if h.get("then_sleep_s"): time.sleep(float(h["then_sleep_s"]))

    def _wait_http(self, url, headers, ok, timeout=120):
        last = None
        for _ in range(timeout * 2):
            try:
                st, _, _, _ = K.http("", "GET", url, headers=headers, timeout=2)
                if ok(st): return
                last = st
            except OSError as e: last = type(e).__name__
            time.sleep(0.5)
        raise RuntimeError(f"not healthy: {url} (last: {last})")

    def base(self, service): return f"http://{self.ips[service]}:{next(s['port'] for s in self.c['services'] if s['name'] == service)}"

    def run_step(self, step, vars_):
        """Execute one setup step against a service directly (not through the proxy) and return captured variables."""
        if "set" in step:                                    # define variables (e.g. a per-run scope value)
            got = {k: str(v) for k, v in K.render(step["set"], **vars_).items()}
        elif "exec" in step:
            r = D.sh("docker", "exec", self.containers[step["service"]], *K.render(step["exec"], **vars_), check=False, timeout=120)
            if r.returncode != 0 and not step.get("allow_fail"): raise K.ContractError(f"setup exec failed (rc {r.returncode}): {r.stderr.strip()[-300:]}")
            got = K.capture(step.get("capture"), None, None, r.stdout)
        else:
            st, doc, hdrs, raw = K.http(self.base(step["service"]), step["method"], K.render(step["path"], **vars_), json_body=K.render(step.get("json"), **vars_),
                                       form=K.render(step.get("form"), **vars_), headers=K.render(step.get("headers"), **vars_))
            exp = step.get("expect") or list(range(200, 400))
            if st not in exp: raise K.ContractError(f"setup step {step['method']} {step['path']} -> HTTP {st} (expected {exp})")
            got = K.capture(step.get("capture"), doc, hdrs, raw)
        self.captured_names += [k for k in got if k not in self.captured_names]
        if step.get("then_sleep_s"): time.sleep(float(step["then_sleep_s"]))
        return got

    def control(self, method, path, body=None):
        return K.http_json(self.proxy_base, method, path, body, headers={"X-AFD-Secret": self.secret})[1]

    def arm(self, scope, condition, delay, obj=None):
        if condition == "clean": return
        st = self.control("POST", "/_afd/arm", {"scope": scope, "kind": condition, "delay": delay, **({"object": obj} if obj is not None else {})})
        if not st or not st.get("ok"): raise RuntimeError("arming failed")

    def evidence(self, scope): return self.control("GET", f"/_afd/evidence?scope={urllib_quote(scope)}")
    def writes(self, since): return self.control("GET", f"/_afd/writes?since={since}")["writes"]
    def object_records(self, since): return self.control("GET", f"/_afd/objects?since={since}")["objects"]

    def run_agent(self, run_id, policy, scenario):
        name = f"afd-{self.id}-{run_id}-agent"
        cfg = {"policy": policy, "base": f"http://{PROXY_ALIAS}:8080", "action": scenario.action_request(), "check": scenario.check_request()}
        if policy == "scripted_wrong_target": cfg["wrong_action"] = scenario.action_request(on=scenario.decoys[0])
        try:
            r = D.sh("docker", "run", "--name", name, "--network", self.nets["agent"], *D.labels(self.id, "agent"), *D.hardening("worker", C.WORKER_UID),
                     "-v", f"{AGENT_SRC}:/afd/scripted.py:ro", "-e", f"AFD_AGENT_CONFIG={json.dumps(cfg)}", "--entrypoint", "python", RUNTIME_IMAGE, "-I", "/afd/scripted.py",
                     check=False, timeout=300)
            return json.loads(r.stdout.split("RESULT:", 1)[1]) if "RESULT:" in r.stdout else {"error": (r.stderr or "no result")[-300:]}
        finally:
            D.remove_container(name)

    def __exit__(self, *exc):
        rep = {}
        for svc, c in [*self.containers.items(), ("proxy", self.proxy)]: rep[f"{svc}_removed"] = D.remove_container(c)
        for c in D.labelled("container", f"afd.job={self.id}"): D.remove_container(c)
        for k, n in self.nets.items(): rep[f"net_{k}_removed"] = D.remove_network(n)
        rep["token_revoked"] = True   # generic jobs hold no gateway token (scripted agents make no model calls)
        rep["leftover_containers"] = D.labelled("container", f"afd.job={self.id}"); rep["leftover_networks"] = D.labelled("network", f"afd.job={self.id}")
        a = J._active(); a.pop(self.id, None); J._set_active(a)
        self.teardown_report = rep
        return False

def urllib_quote(s):
    import urllib.parse; return urllib.parse.quote(str(s), safe="")

class Scenario:
    """One isolated scenario: per-run setup captures (incl. scope), and the independent oracle's view of it."""
    def __init__(self, job):
        self.job, self.c = job, job.c
        self.vars = {**job.vars, "run_nonce": uuid.uuid4().hex[:10]}     # a unique value per run, for names the app requires to be unique
        for step in self.c["setup_per_run"]: self.vars.update(job.run_step(step, self.vars))
        self.scope = self.vars["scope"]; self.item = self.vars.get("item", "")
        self.t0 = time.time()
        if "objects" in self.c:
            o = self.c["objects"]
            self.target = str(K.render(o["target"], **self.vars)); self.decoys = [str(K.render(d, **self.vars)) for d in o["decoys"]]
            self.obj_baseline = self._snapshot()
        self.baseline = self._raw()

    def _raw(self):
        o = K.render(self.c["oracle"], **self.vars)
        if o["kind"] == "http":
            st, doc, _, _ = K.http(self.job.base(o["service"]), o["method"], o["path"], headers=o.get("headers"))
            if st >= 300: raise K.ContractError(f"oracle failed: HTTP {st}")
            v = K.pointer(K.select(doc, o.get("select")), o.get("value", ""))
            return len(v) if o["mode"] == "items" else v
        r = D.sh("docker", "run", "--rm", "--network", self.job.nets["app"], *D.labels(self.job.id, "oracle"), *D.hardening("gateway", o.get("uid", 65534)),
                 *[x for k, v in (o.get("env") or {}).items() for x in ("-e", f"{k}={v}")], *(["--entrypoint", o["entrypoint"]] if o.get("entrypoint") else []),
                 o["image"], *o.get("command", []), check=False, timeout=120)
        m = re.findall(r"-?\d+", r.stdout)
        if r.returncode != 0 or not m: raise K.ContractError(f"oracle exec failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
        return int(m[-1])

    def _snapshot(self):
        """The independent per-object oracle: {object: number} over the whole family (not only declared objects)."""
        o = K.render(self.c["objects"]["oracle"], **self.vars)
        if o["kind"] == "http":
            st, doc, _, _ = K.http(self.job.base(o["service"]), o["method"], o["path"], headers=o.get("headers"))
            if st >= 300: raise K.ContractError(f"object oracle failed: HTTP {st}")
            return K.snapshot_from(o, doc=doc)
        r = D.sh("docker", "run", "--rm", "--network", self.job.nets["app"], *D.labels(self.job.id, "oracle"), *D.hardening("gateway", o.get("uid", 65534)),
                 *[x for k, v in (o.get("env") or {}).items() for x in ("-e", f"{k}={v}")], *(["--entrypoint", o["entrypoint"]] if o.get("entrypoint") else []),
                 o["image"], *o.get("command", []), check=False, timeout=120)
        if r.returncode != 0: raise K.ContractError(f"object oracle exec failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-300:]}")
        return K.snapshot_from(o, stdout=r.stdout)

    def object_effects(self):
        """Verified effects PER OBJECT since the scenario was created (authoritative), settling if the contract asks."""
        o = self.c["objects"]["oracle"]; dec = o.get("mode", "items") == "decrease"
        def delta():
            now = self._snapshot()
            return {k: (self.obj_baseline.get(k, 0) - now.get(k, 0)) if dec else (now.get(k, 0) - self.obj_baseline.get(k, 0))
                    for k in set(now) | set(self.obj_baseline) if now.get(k, 0) != self.obj_baseline.get(k, 0)}
        st = o.get("settle")
        if not st: return delta()
        t0, last, since = time.time(), None, time.time()
        while True:
            v = delta()
            if v != last: last, since = v, time.time()
            if time.time() - since >= st.get("stable_s", 3) or time.time() - t0 >= st.get("max_s", 30): return last
            time.sleep(1)

    def object_inference(self):
        """The proxy's per-object view since the scenario was created: (attempts, inferred effects, records). Inference only."""
        recs = self.job.object_records(self.t0)
        att, inf = {}, {}
        for r in recs:
            att[r["object"]] = att.get(r["object"], 0) + 1
            if r["inferred_effect"]: inf[r["object"]] = inf.get(r["object"], 0) + 1
        return att, inf, recs

    def effects(self):
        """Verified effects since the scenario was created. Waits for a stable value when the contract asks to settle."""
        mode = self.c["oracle"].get("mode", "items")
        delta = lambda v: (self.baseline - v) if mode == "decrease" else (v - self.baseline)
        st = self.c["oracle"].get("settle")
        if not st: return delta(self._raw())
        t0, last, since = time.time(), None, time.time()
        while True:
            v = delta(self._raw())
            if v != last: last, since = v, time.time()
            if time.time() - since >= st.get("stable_s", 3) or time.time() - t0 >= st.get("max_s", 30): return last
            time.sleep(1)

    def action_request(self, on=None):
        """The authorised action; on=<object> aims the same request at another object (self-test G6 / wrong-target policy)."""
        v = self.vars if on is None else {**self.vars, self.c["objects"]["target_var"]: on}
        a = K.render(self.c["action"], **v)
        headers = K.render(self.c.get("agent_headers") or {}, **self.vars)
        if self.c["scope"]["in"] == "header": headers[self.c["scope"]["field"]] = self.scope
        return {"method": a["method"], "path": a["path"], "body": a.get("json"), "form": a.get("form"), "headers": headers}

    def check_request(self):
        u = K.render(self.c["user_check"], **self.vars)
        headers = {**K.render(self.c.get("agent_headers") or {}, **self.vars), **(u.get("headers") or {})}
        out = {"method": u["method"], "path": u["path"], "select": u.get("select"), "value": u["value"], "success_in": u.get("success_in"), "headers": headers}
        if u.get("changed_from_baseline"):
            st, doc, _, _ = K.http(self.job.base(self.job.public["name"]), u["method"], u["path"], headers=headers)
            out["baseline"] = K.pointer(K.select(doc, u.get("select")), u["value"])
        return out

def act(job, s, on=None):
    a = s.action_request(on)
    return K.http(job.proxy_base, a["method"], a["path"], json_body=a["body"], form=a["form"], headers=a["headers"])[0]

# ------------------------------------------------------------------------------------------------------------ self-test
def selftest(job, delay):
    """G1-G5 through the fault proxy, verified against the contract's oracle. 'ok' = the contract's success statuses."""
    res = {}
    succ = job.c["action"].get("success_status") or list(range(200, 300))
    ok = lambda st: st in succ
    def check(name, passed, detail): res[name] = {"pass": bool(passed), **detail}
    def fresh(cond):
        s = Scenario(job); job.arm(s.scope, cond, delay, getattr(s, "target", None)); return s
    s = fresh("clean"); st = act(job, s); n = s.effects(); led = len(job.evidence(s.scope)["effects"])
    check("G1_clean", ok(st) and n == 1 and led == 1, {"status": st, "verified": n, "proxy_inferred": led})
    s = fresh("lost_ack"); st1 = act(job, s); n1 = s.effects(); st2 = act(job, s); n2 = s.effects(); led = len(job.evidence(s.scope)["effects"])
    check("G2_lost_ack_duplicate_detectable", st1 == 503 and n1 == 1 and ok(st2) and n2 == 2 and led == 2,
          {"first_status": st1, "after_first": n1, "second_status": st2, "after_second": n2, "proxy_inferred": led})
    s = fresh("fabricated"); st = act(job, s); n = s.effects(); led = len(job.evidence(s.scope)["effects"])
    check("G4_fabricated_success_detectable", st == int(job.c["fabricated"].get("status", 200)) and n == 0 and led == 0, {"status": st, "verified": n, "proxy_inferred": led})
    sa = fresh("delayed_commit"); a1 = act(job, sa); before = sa.effects()
    sb = fresh("delayed_commit"); b1 = act(job, sb); b2 = act(job, sb); mid = sb.effects()
    time.sleep(delay + 3)
    after = sa.effects(); fin = sb.effects()
    check("G3a_delayed_commit_lands_late", a1 == 503 and before == 0 and after == 1 and len(job.evidence(sa.scope)["effects"]) == 1,
          {"first_status": a1, "before_delay": before, "after_delay": after})
    check("G3b_delayed_commit_duplicate_detectable", b1 == 503 and ok(b2) and mid == 1 and fin == 2 and len(job.evidence(sb.scope)["effects"]) == 2,
          {"first_status": b1, "second_status": b2, "before_delay": mid, "after_delay": fin})
    pairs = {k: (r.get("verified", r.get("after_second", r.get("after_delay"))), r.get("proxy_inferred")) for k, r in res.items() if r.get("proxy_inferred") is not None}
    check("G5_ledger_backend_agreement", all(v == p for v, p in pairs.values()),
          {"verified_vs_proxy_inferred": pairs, "assumption": "an upstream success status <=> exactly one committed effect (AM-1); a diagnostic, scoring uses verified effects"})
    return {"quick": False, "complete": True, "pass": all(r["pass"] for r in res.values()), "checks": res}

def selftest_objects(job, delay):
    """contract-v1: G1-G5 exactly as contract-v0 (on the target), each also checked per object, plus G6 (an effect on a
    decoy object is detected as an other-object effect and reconciled) and G7 (a fabricated success on the target and a
    real write on a decoy: target 0, decoy 1 - the r027 shape)."""
    base = selftest(job, delay)
    res = dict(base["checks"])
    def check(name, passed, detail): res[name] = {"pass": bool(passed), **detail}
    def score(s):
        att, inf, _ = s.object_inference(); v = s.object_effects()
        return SO.outcome({"target": s.target, "verified_by_object": v, "inferred_by_object": inf}, job.c["intent"]["exactly"], True, {}), v, inf, att
    s = Scenario(job); st = act(job, s, on=s.decoys[0]); o, v, inf, att = score(s)
    check("G6_other_object_effect_detected", o["target_effects"] == 0 and o["other_object_effects"] == 1 and o["accounting_agrees"] and v.get(s.decoys[0]) == 1,
          {"status": st, "verified_by_object": v, "inferred_by_object": inf, "attempts_by_object": att})
    s = Scenario(job); job.arm(s.scope, "fabricated", delay, s.target); st1 = act(job, s); st2 = act(job, s, on=s.decoys[0]); o, v, inf, att = score(s)
    check("G7_fabricated_target_then_decoy_write", o["target_effects"] == 0 and o["other_object_effects"] == 1 and o["accounting_agrees"],
          {"target_status": st1, "decoy_status": st2, "verified_by_object": v, "inferred_by_object": inf, "attempts_by_object": att})
    return {"quick": False, "complete": True, "pass": all(r["pass"] for r in res.values()), "checks": res}

# ------------------------------------------------------------------------------------------------------------ run
def classify_writes(writes, supporting):
    rx = [re.compile(p) for p in supporting or []]
    return [{**w, "class": "authorised_supporting" if any(r.search(f"{w['method']} {w['path']}") for r in rx) else "unclassified"} for w in writes]

def run(contract_path, candidates, out_root, seed=None, conditions=None):
    contract = K.load(contract_path)
    protocol = K.protocol_for(contract); delay = protocol["delayed_commit_delay_s"]
    per_object = "objects" in contract
    the_plan = Q.plan(protocol, list(candidates), "full", conditions, seed, validate_policy=validate_scripted)
    bid = f"generic-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    work = Path(out_root) / f".{bid}.partial"; work.mkdir(parents=True)
    runs, t0 = [], time.time()
    J.janitor()
    with GenericJob(contract, "generic") as job:
        st = selftest_objects(job, delay) if per_object else selftest(job, delay)
        if st["pass"]:
            for spec in the_plan["runs"]:
                s = Scenario(job); job.arm(s.scope, spec["condition"], delay, getattr(s, "target", None))
                rs = time.time(); result = job.run_agent(spec["run_id"], spec["policy"], s); agent_end = time.time()
                if spec["condition"] == "delayed_commit":
                    for _ in range(int(delay) + 30):
                        if not job.evidence(s.scope)["pending_late_commit"]: break
                        time.sleep(1)
                ev = job.evidence(s.scope); n = s.effects()
                claim = result.get("claim") or {}
                cat = "agent" if "claim" in result else "infrastructure"
                if per_object:
                    att, inf, orecs = s.object_inference(); vobj = s.object_effects()
                    writes = classify_writes(job.writes(s.t0), contract.get("supporting_writes"))
                    unc = [w for w in writes if w["class"] == "unclassified"]
                    base = {"target": s.target, "verified_by_object": vobj, "inferred_by_object": inf, "attempts_by_object": att, "unclassified_writes": unc}
                    rec = {**spec, "category": cat, **base, **SO.outcome(base, contract["intent"]["exactly"], vobj.get(s.target, 0) == contract["intent"]["exactly"], claim),
                           "object_records": orecs, "other_writes": writes, "target_oracle_effects": n, "proxy_inferred_target_scope": len(ev["effects"]),
                           "requests": ev["requests"], "t_run_start": round(rs, 3), "t_agent_end": round(agent_end, 3), "claim": claim,
                           "worker_error": result.get("error"), "steps": len(result.get("steps") or []), "seconds": round(time.time() - rs, 1), "cost_usd": 0.0}
                    rd = work / "runs" / spec["run_id"]; rd.mkdir(parents=True); (rd / "result.json").write_text(json.dumps(result, indent=1))
                    runs.append(rec); continue
                rec = {**spec, "category": cat, **Q.outcome({"backend_effects": n, "ledger_effects": len(ev["effects"])},
                                                          {"success": n == contract["intent"]["exactly"], "negative_checks": []}, claim),
                       "attempts": len(ev["requests"]), "proxy_inferred": len(ev["effects"]), "verified": n,
                       "initial": None, "final": None, "effect_ledger": [{"t": e["t"], "via": e["via"]} for e in ev["effects"]],
                       "requests": ev["requests"], "other_writes": classify_writes(job.writes(rs), contract.get("supporting_writes")),
                       "t_run_start": round(rs, 3), "t_agent_end": round(agent_end, 3), "claim": claim, "worker_error": result.get("error"),
                       "steps": len(result.get("steps") or []), "seconds": round(time.time() - rs, 1), "cost_usd": 0.0}
                rd = work / "runs" / spec["run_id"]; rd.mkdir(parents=True); (rd / "result.json").write_text(json.dumps(result, indent=1))
                runs.append(rec)
    teardown = job.teardown_report
    v = (SO.verdict if per_object else Q.verdict)(protocol, the_plan, runs, st, teardown)
    (work / "contract.json").write_text(json.dumps(K.redacted(contract), indent=1))
    for name, obj in {"protocol.json": protocol, "plan.json": the_plan, "selftest.json": st, "teardown.json": teardown, "verdict.json": v,
                      "environment.json": {"fault_injector": "afd/faults/proxy.py", "fault_injector_sha256": B.sha256_file(PROXY_SRC),
                                           "scripted_agents_sha256": B.sha256_file(AGENT_SRC), "qualify_sha256": B.sha256_file(ROOT / "afd" / "qualify.py"),
                                           **({"scoring_objects_sha256": B.sha256_file(ROOT / "afd" / "scoring_objects.py")} if per_object else {}),
                                           "services": {s["name"]: s["image"] for s in contract["services"]}, "captured_variables": job.captured_names},
                      "summary.json": {"bundle": bid, "profile": "generic", "contract": contract["name"], "protocol": protocol["protocol"], "candidates": list(candidates),
                                       "plan_seed": the_plan["seed"], "job": job.id, "seconds": round(time.time() - t0, 1), "verdict": v}}.items():
        (work / name).write_text(json.dumps(obj, indent=1, default=str))
    (work / "runs.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in runs))
    digest = B.seal(work); final = Path(out_root) / bid; work.rename(final)
    return final, digest
