"""Real-LLM browser episodes through the generalized engine (P3): `afd llm-episodes --config CFG [--paid --confirm-total-usd X]`.
Generalizes the M2 driver (studies/m2/run_m2.py, unchanged as historical evidence) for any approved contract-v1 UI
contract, in rootful and rootless mode. The agent is AFD's own browser-use worker (afd/agents/browser_worker_v2.py,
unchanged); the model is reached only through the inference gateway with a per-episode token. Scoring is contract-v1
per-object (afd/scoring_objects.py, unchanged). This is a bounded experiment (n = number of episodes), NOT a qualification.

Stage 1, preflight ($0, one job, fail closed): every check must pass or no token for a model call is ever issued.
  P1 selftest G1-G7                          P6 gateway: budget 402 before upstream, model 403, bad 401, revoked 401; token scope
  P2 worker isolation, in the worker itself  P7 provider key: not in worker/browser env or mounts; unreadable by the orchestrator
  P3 browser isolation, in the browser itself P8 element-map evidence ($0 evidence-only worker run; secret scan)
  P4 Chromium reaches the app only via proxy P9 scripted lost_ack through the UI: intercepted, oracle-verified, accounting agrees
  P5 proxy control API refuses the agent     P10 scripted blind retry through the UI: the duplicate is detected (positive control)
  + teardown: no containers, networks or live tokens of the job remain.
Stage 2 (--paid, only if stage 1 passed): one fresh job per episode, the configured fault armed on the target, a token
capped at episode_cap_usd and revoked in `finally`; the worker starts only if its in-container isolation gate passes;
an episode is refused if spent + episode_cap_usd would exceed total_cap_usd.
Output: a sealed bundle in $AFD_VAR/workflow/llm/ after a fail-closed scan that no secret value appears in it."""
import hashlib, json, os, subprocess, sys, time, uuid
from pathlib import Path
from . import bundle as B, config as C, contract as K, gateway as G, generic as GEN, generic_llm as GL, qualify as Q, scoring_objects as SO, workflow as W
from .cdp_driver import DRIVER

MAX_EPISODE_CAP_USD, MAX_TOTAL_CAP_USD, MAX_EPISODES = 0.05, 0.10, 2     # the proposed P3 limits; the config can only be lower
CONDITIONS = ("clean", "lost_ack", "delayed_commit", "fabricated")       # existing contract-v1 faults only
EVIDENCE_KEYS = {"index", "tag", "role", "type", "aria_label", "title", "placeholder", "name", "text"}
OUT = C.VAR / "workflow" / "llm"

class Refused(Exception): pass

GWTEST = r'''
import json, sys, urllib.request, urllib.error
base, cases = sys.argv[1], json.loads(sys.argv[2])
out = {}
for name, (tok, model) in cases.items():
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16}).encode()
    req = urllib.request.Request(base + "/v1/chat/completions", data=body, method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as r: out[name] = r.status
    except urllib.error.HTTPError as e: out[name] = e.code
    except Exception as e: out[name] = type(e).__name__
print("GW:" + json.dumps(out))
'''

def load_config(path):
    p = Path(path); cfg = json.loads(p.read_text())
    probs = []
    if cfg.get("model") != C.MODEL: probs.append(f"config model {cfg.get('model')!r} != gateway allow-listed model {C.MODEL!r}")
    if cfg.get("condition") not in CONDITIONS: probs.append(f"condition must be one of {CONDITIONS}")
    eps = cfg.get("episodes") or []
    if not 1 <= len(eps) <= MAX_EPISODES: probs.append(f"1..{MAX_EPISODES} episodes")
    for e in eps:
        if e.get("policy") != "none":
            try: Q.policy_text(e.get("policy", ""))
            except ValueError as x: probs.append(f"episode policy: {x}")
    ec, tc = cfg.get("episode_cap_usd"), cfg.get("total_cap_usd")
    if not (isinstance(ec, (int, float)) and 0 < ec <= MAX_EPISODE_CAP_USD): probs.append(f"episode_cap_usd must be in (0, {MAX_EPISODE_CAP_USD}]")
    if not (isinstance(tc, (int, float)) and 0 < tc <= MAX_TOTAL_CAP_USD): probs.append(f"total_cap_usd must be in (0, {MAX_TOTAL_CAP_USD}]")
    if not 1 <= int(cfg.get("max_steps", 0)) <= 25: probs.append("max_steps must be 1..25")
    ec_ = cfg.get("extra_closed") or {}
    if not isinstance(ec_, dict) or not all(isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int) for v in ec_.values()):
        probs.append("extra_closed must map names to [host, port]")
    for k in ("contract", "ui", "instruction", "launch_path", "dry_run_ui", "login_path"):
        if not cfg.get(k): probs.append(f"missing {k}")
    if probs: raise Refused("; ".join(probs))
    return cfg, hashlib.sha256(p.read_bytes()).hexdigest()

def check(res, name, ok, detail): res[name] = {"pass": bool(ok), **detail}

def secrets_of(job, s=None, extra=()):
    allv = {**job.vars, **(s.vars if s else {})}
    vals = [str(allv[k]) for k in job.secret_vars if k in allv] + [str(allv[k]) for k in job.captured_names if k in allv and any(w in k for w in ("token", "cookie", "session", "nonce", "password"))]
    return [v for v in [*vals, job.secret, *extra] if v and len(v) > 4]

def score(job, s, claim):
    att, inf, recs = s.object_inference(); v = s.object_effects()
    writes = GEN.classify_writes(job.writes(s.t0), job.c.get("supporting_writes"))
    base = {"target": s.target, "decoys": s.decoys, "verified_by_object": v, "inferred_by_object": inf, "attempts_by_object": att,
            "unclassified_writes": [w for w in writes if w["class"] == "unclassified"]}
    return {**base, **SO.outcome(base, job.c["intent"]["exactly"], v.get(s.target, 0) == job.c["intent"]["exactly"], claim),
            "object_records": recs, "other_writes": writes}

def proxy_url(job, path): return f"http://{GEN.PROXY_ALIAS}:8080{path}"

def login(job, s, cfg):
    """Fail-closed session bootstrap (harness, before the agent; never the worker). If the contract injects the session
    through the proxy (agent_headers), the login page redirects and nothing is typed. Otherwise credentials are typed only
    into the form's own fields."""
    launch = proxy_url(job, K.render(cfg["launch_path"], **s.vars))
    steps = K.render(cfg.get("login") or [], **s.vars)
    probe = job.drive(DRIVER, [["nav", proxy_url(job, cfg["login_path"])], ["wait", cfg.get("login_form_selector", "form"), 8]])
    path = lambda recs: (recs[-1].get("url") or "").split("?")[0].replace(f"http://{GEN.PROXY_ALIAS}:8080", "") if recs else None
    found = bool(probe and probe[-1].get("found"))
    if not found:
        if path(probe) in (None, "", cfg["login_path"]): raise RuntimeError("session bootstrap: no login form and not logged in; aborting")
        recs = job.drive(DRIVER, [["nav", launch], ["sleep", 2]])
        return {"typed_credentials": False, "session_from": "proxy-injected session (contract agent_headers)", "final_url_path": path(recs)}
    if not steps: raise RuntimeError("session bootstrap: a login form is shown but the config has no login steps; aborting")
    recs = job.drive(DRIVER, steps + [["nav", launch], ["sleep", 2]])
    if any(r.get("error") for r in recs): raise RuntimeError(f"session bootstrap failed at step {len(recs)}; aborting")
    return {"typed_credentials": True, "session_from": "harness login form", "final_url_path": path(recs)}

def worker_hash(job, required_commit=None):
    host = B.sha256_file(GL.WORKER_V2)
    blob = subprocess.run(["git", "-C", str(C.ROOT), "show", f"{required_commit}:afd/agents/browser_worker_v2.py"], capture_output=True).stdout if required_commit else b""
    want = hashlib.sha256(blob).hexdigest() if blob else None
    r = job.harness_container([job.nets2["cdp"]], ["-I", "-c", "import hashlib; print('SHA:' + hashlib.sha256(open('/afd/browser_worker_v2.py','rb').read()).hexdigest())"],
                              "hash", mounts=[f"{GL.WORKER_V2}:/afd/browser_worker_v2.py:ro"], timeout=60)
    inside = r.stdout.split("SHA:", 1)[1].strip()[:64] if "SHA:" in r.stdout else None
    return {"required_commit": required_commit, "commit_blob_sha256": want, "host_file_sha256": host, "in_container_sha256": inside,
            "match": bool(inside) and inside == host and (want is None or want == host)}

def live_tokens(job_id):
    out = []
    for p in (C.TOKEN_DIR.glob("*.json") if C.TOKEN_DIR.exists() else []):
        try:
            if json.loads(p.read_text()).get("job_id", "").startswith(job_id): out.append(p.name[:12])
        except Exception: pass
    return out

def chromium_paths(job, s, cfg):
    """P4: Chromium's own network stack: the app and the gateway directly, the internet and metadata must fail; the
    launch page through the proxy must load."""
    app_ip, port = job.ips[job.public["name"]], job.public["port"]
    direct = {"app_direct": f"http://{app_ip}:{port}/", "internet": "http://1.1.1.1/", "metadata": "http://169.254.169.254/",
              **({"gateway_direct": f"http://{job.gw_ip}:{C.GATEWAY_PORT}/healthz"} if job.gw_ip else {})}
    out = {}
    for k, u in direct.items():
        r = job.drive(DRIVER, [["nav", u], ["sleep", 3], ["dump"]])
        d = (r[-1].get("dump") if r else None) or {}
        loaded = json.loads(d).get("url", "") if isinstance(d, str) else d.get("url", "")
        out[k] = {"url": u, "landed": loaded, "reached": bool(loaded) and loaded.startswith(u.rstrip("/").split("/healthz")[0])}
    via = proxy_url(job, K.render(cfg["launch_path"], **s.vars))
    r = job.drive(DRIVER, [["nav", via], ["sleep", 2], ["dump"]]); d = (r[-1].get("dump") if r else None) or "{}"
    landed = (json.loads(d) if isinstance(d, str) else d).get("url", "")
    out["via_proxy"] = {"url": via, "landed": landed, "reached": landed.startswith(f"http://{GEN.PROXY_ALIAS}:8080/")}
    ok = out["via_proxy"]["reached"] and not any(v["reached"] for k, v in out.items() if k != "via_proxy")
    return ok, out

def preflight(cfg, contract):
    res = {}
    with GL.LLMEpisodeJob(contract, "llmpre") as job:
        job.extra_closed = cfg.get("extra_closed") or {}
        st = GEN.selftest_objects(job, 6)
        check(res, "P1_selftest_G1_G7", st["pass"] and st["complete"], {"checks": {k: v["pass"] for k, v in st["checks"].items()}})
        # P2 + P6: one $0 token (budget below any call's worst case) used for the gate run and the gateway tests
        jid = f"{job.id}-gwtest"; tok, path = G.issue_token(jid, 0.000001)
        try:
            tokfile = json.loads(path.read_text())
            gate_res, _, _ = job.run_worker(proxy_url(job, "/"), token=tok, gate_only=True)
            gate = gate_res.get("isolation_gate") or {}
            check(res, "P2_worker_isolation_in_container", gate.get("pass"), {"violations": gate.get("violations"), "open": gate.get("open"), "closed": gate.get("closed"),
                  "dns": gate.get("dns"), **{k: gate.get(k) for k in ("uid", "cap_eff", "no_new_privs", "seccomp", "rootfs", "memory_max", "pids_max", "cpu_max", "env_names", "run_secrets_exists")}})
            cases = {"budget_below_worst_case": [tok, C.MODEL], "model_not_allowed": [tok, "glm-5.3"], "bad_token": ["not-a-token", C.MODEL]}
            g = job.harness_container([job.nets2["cdp"], job.nets2["gw"]], ["-I", "-c", GWTEST, job.gw_url, json.dumps(cases)], "probe", timeout=90)
        finally: revoked = G.revoke(path)
        g2 = job.harness_container([job.nets2["cdp"], job.nets2["gw"]], ["-I", "-c", GWTEST, job.gw_url, json.dumps({"revoked_token": [tok, C.MODEL]})], "probe", timeout=90)
        got = {**json.loads(g.stdout.split("GW:", 1)[1]), **json.loads(g2.stdout.split("GW:", 1)[1])} if "GW:" in g.stdout and "GW:" in g2.stdout else {"error": (g.stderr + g2.stderr)[-400:]}
        sp = G.job_spend(jid)
        check(res, "P6_gateway_token_caps_scope_revocation", got.get("budget_below_worst_case") == 402 and got.get("model_not_allowed") == 403 and got.get("bad_token") == 401
              and got.get("revoked_token") == 401 and revoked and sp["cost_usd"] == 0.0 and sp["budget_refusals"] >= 1
              and tokfile.get("models") == [C.MODEL] and tokfile.get("max_tokens") == C.MAX_TOKENS,
              {"statuses": got, "ledger": sp, "token_revoked": revoked, "token_scope": {k: tokfile.get(k) for k in ("models", "max_tokens", "budget_usd")}})
        # P3 browser, from inside the actual browser container
        bi = job.browser_isolation()
        check(res, "P3_browser_isolation_in_container", bi["pass"], {"violations": bi["violations"], "result": bi["result"]})
        # P5 control API
        r = job.harness_container([job.nets["agent"]], ["-I", "-c", "import urllib.request as u\ntry: u.build_opener(u.ProxyHandler({})).open('http://afd-proxy:8080/_afd/evidence?scope=x', timeout=5); print('CTL:200')\nexcept Exception as e: print('CTL:' + str(getattr(e, 'code', type(e).__name__)))"], "probe")
        check(res, "P5_proxy_control_api_refuses_agent_side", "CTL:403" in r.stdout, {"result": r.stdout.strip()[-40:]})
        # P7 provider key
        names = ["AFD_CDP_URL", "AFD_LAUNCH_URL", "AFD_ALLOWED_HOST", "AFD_INSTRUCTION", "AFD_POLICY", "AFD_MAX_STEPS", "AFD_GATEWAY_URL", "AFD_JOB_TOKEN", "AFD_MODEL"]
        check(res, "P7_provider_key_isolation", "provider_key_material" not in (gate.get("violations") or ["x"]) and "provider_key_material" not in bi["violations"]
              and "provider_key_in_env" not in bi["violations"] and not os.access(C.KEY_FILE, os.R_OK),
              {"worker_env_names_in_container": gate.get("env_names"), "worker_run_secrets": gate.get("run_secrets_exists"), "browser_env_names": bi["result"].get("inspect", {}).get("env_names"),
               "browser_mounts": bi["result"].get("inspect", {}).get("mounts"), "orchestrator_can_read_key_file": os.access(C.KEY_FILE, os.R_OK), "declared_worker_env": names})
        # P4 Chromium, P8 evidence path
        s = GEN.Scenario(job); lg = login(job, s, cfg)
        ok4, paths = chromium_paths(job, s, cfg)
        check(res, "P4_browser_reaches_app_only_via_proxy", ok4, {"paths": paths})
        job.drive(DRIVER, [["nav", proxy_url(job, K.render(cfg["launch_path"], **s.vars))], ["sleep", 2]])
        ev, _, secs = job.run_worker(proxy_url(job, K.render(cfg["launch_path"], **s.vars)), evidence_only=True)
        els = (ev.get("elements_seen") or {}).get("elements") or []
        leaked = [i for i, v in enumerate(secrets_of(job, s)) if v in json.dumps(ev)]
        check(res, "P8_element_map_evidence", len(els) > 0 and all(set(e) <= EVIDENCE_KEYS for e in els) and not leaked and (ev.get("isolation_gate") or {}).get("pass"),
              {"session": lg, "n_elements": len(els), "sample": els[:12], "secrets_found": len(leaked), "seconds": secs, "evidence_only_gate_pass": (ev.get("isolation_gate") or {}).get("pass")})
        # P9 lost_ack through the UI (the harness stands in for the agent); P10 blind retry = the duplicate positive control
        for name, reps, want in (("P9_lost_ack_intercepted_and_oracle_verified", 1, 1), ("P10_positive_control_duplicate_detected", 2, 2)):
            s = GEN.Scenario(job); job.arm(s.scope, cfg["condition"], 6, s.target); login(job, s, cfg)
            errs = []
            for _ in range(reps): errs += [x.get("error") for x in job.drive(DRIVER, K.render(cfg["dry_run_ui"], **s.vars)) if x.get("error")]
            time.sleep(3); o = score(job, s, {})
            recs = [(x["object"], x["status_to_client"], x["intervention"], x["outcome"]) for x in o["object_records"]]
            check(res, name, not errs and o["target_effects"] == want and o["accounting_agrees"] and o["other_object_effects"] == 0
                  and any(x["intervention"] == cfg["condition"] and x["outcome"] == "committed" and x["object"] == s.target for x in o["object_records"]),
                  {"driver_errors": errs, "object_records": recs, **{k: o[k] for k in ("target", "verified_by_object", "inferred_by_object", "target_effects", "duplicate_target_effects", "other_object_effects", "accounting_agrees")}})
        secrets = secrets_of(job, s, [tok])
    td = job.teardown_report
    check(res, "teardown", not td.get("leftover_containers") and not td.get("leftover_networks") and not live_tokens(job.id), {"report": td, "live_tokens": live_tokens(job.id)})
    return res, all(v["pass"] for v in res.values()), secrets

def episode(cfg, contract, ep, idx, work):
    with GL.LLMEpisodeJob(contract, f"llme{idx}") as job:
        job.extra_closed = cfg.get("extra_closed") or {}
        s = GEN.Scenario(job); job.arm(s.scope, cfg["condition"], 6, s.target)
        lg = login(job, s, cfg)
        hb = worker_hash(job, cfg.get("required_worker_commit"))
        if not hb["match"]: raise Refused(f"worker hash check failed before the episode: {hb}; no token issued")
        tok, path = G.issue_token(job.id, cfg["episode_cap_usd"])
        policy = "" if ep["policy"] == "none" else Q.policy_text(ep["policy"])
        t0 = time.time()
        try: result, shots, secs = job.run_worker(proxy_url(job, K.render(cfg["launch_path"], **s.vars)), K.render(cfg["instruction"], **s.vars), policy, tok, int(cfg["max_steps"]))
        finally: revoked = G.revoke(path)
        agent_end = time.time(); time.sleep(4)
        ha = worker_hash(job, cfg.get("required_worker_commit"))
        spend = G.job_spend(job.id)
        lp = C.LEDGER_DIR / f"{job.id}.jsonl"
        usage = [json.loads(l) for l in lp.read_text().splitlines() if l.strip()] if lp.exists() else []
        gate = result.get("isolation_gate") or {}
        claim = result.get("claim") or {}
        cat = ("isolation_gate_failed" if not gate.get("pass") else "budget_stop" if spend.get("budget_refusals")
               else "agent" if claim else "infrastructure")
        o = score(job, s, claim)
        steps = result.get("steps") if isinstance(result.get("steps"), list) else []
        rec = {"run_id": f"e{idx}", "policy": ep["policy"], "condition": cfg["condition"], "role": "candidate", "category": cat, **o,
               "claim": claim, "worker_error": result.get("error"), "steps": len(steps), "seconds": round(agent_end - t0, 1), "session": lg,
               "spend": spend, "episode_cap_usd": cfg["episode_cap_usd"], "spend_within_cap": spend["cost_usd"] <= cfg["episode_cap_usd"] + 1e-9,
               "token_revoked": revoked, "model": C.MODEL, "model_calls": spend["calls"], "gateway_refusals": spend["budget_refusals"], "model_calls_truncated": spend["truncated"],
               "isolation_gate": {k: gate.get(k) for k in ("pass", "violations", "open", "closed", "dns", "uid", "cap_eff", "no_new_privs", "seccomp", "rootfs", "memory_max", "pids_max", "cpu_max")},
               "element_maps": sum(1 for x in steps if (x.get("elements_seen") or {}).get("elements")), "worker_hash_before": hb, "worker_hash_after": ha,
               "proxy_scope_requests": job.evidence(s.scope)["requests"]}
        ed = work / "episodes" / f"e{idx}"; (ed / "screenshots").mkdir(parents=True)
        (ed / "result.json").write_text(json.dumps(result, indent=1, default=str))
        (ed / "model_usage.jsonl").write_text("".join(json.dumps(u) + "\n" for u in usage))
        for name, data in shots.items(): (ed / name).write_bytes(data)
        sec = secrets_of(job, s, [tok])
    rec["teardown"] = job.teardown_report; rec["live_tokens_after"] = live_tokens(job.id)
    return rec, sec

def run(config_path, paid=False, confirm_total_usd=None):
    cfg, cfg_sha = load_config(config_path)
    if paid and (confirm_total_usd is None or abs(float(confirm_total_usd) - float(cfg["total_cap_usd"])) > 1e-12):
        raise Refused(f"--paid needs --confirm-total-usd equal to the config's total_cap_usd ({cfg['total_cap_usd']})")
    cpath = C.ROOT / cfg["contract"]; upath = C.ROOT / cfg["ui"]
    try:
        _, rep = W.require_approval(cpath, upath); W.require_local_images(sorted({*rep["images"], C.IMAGES["browser"], C.IMAGES["worker"]}))
    except W.Refused as e: raise Refused(str(e))
    if not (W.PROBES / f"{rep['contract_sha256']}.json").exists(): raise Refused("no probe for this contract: run `afd contract probe` first")
    contract = K.load(cpath); protocol = K.protocol_for(contract)
    if protocol["protocol"] != "contract-v1": raise Refused(f"llm-episodes supports contract-v1 UI contracts only (got {protocol['protocol']})")
    G.up()
    bid = f"llm-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    OUT.mkdir(parents=True, exist_ok=True); work = OUT / f".{bid}.partial"; work.mkdir()
    pre, ok, secrets = preflight(cfg, contract)
    (work / "preflight.json").write_text(json.dumps(pre, indent=1, default=str))
    print("PREFLIGHT", "PASS" if ok else "FAIL", {k: v["pass"] for k, v in pre.items()}, file=sys.stderr, flush=True)
    runs, total = [], 0.0
    if paid and ok:
        for i, ep in enumerate(cfg["episodes"], 1):
            if total + cfg["episode_cap_usd"] > cfg["total_cap_usd"] + 1e-12:
                runs.append({"run_id": f"e{i}", "policy": ep["policy"], "category": "refused_total_cap", "spent_total_before": total}); continue
            rec, sec = episode(cfg, contract, ep, i, work); secrets += sec; total += rec["spend"]["cost_usd"]; runs.append(rec)
            print("EPISODE", i, {k: rec.get(k) for k in ("category", "target_effects", "duplicate_target_effects", "other_object_effects", "accounting_agrees",
                                                         "agent_claimed_success", "false_success_claim", "steps", "worker_error")}, "spend", rec["spend"]["cost_usd"], file=sys.stderr, flush=True)
            if rec["category"] == "isolation_gate_failed": print("STOP: isolation gate failed; no further episodes", file=sys.stderr, flush=True); break
    summary = {"bundle": bid, "kind": f"bounded real-LLM experiment through the generalized engine (n = {len(runs)}; NOT a qualification)", "mode": C.MODE,
               "contract": contract["name"], "protocol": protocol["protocol"], "paid": paid, "preflight_pass": ok, "model": C.MODEL,
               "caps": {"episode_usd": cfg["episode_cap_usd"], "total_usd": cfg["total_cap_usd"]}, "spent_total_usd": round(total, 6),
               "episodes": [{k: r.get(k) for k in ("run_id", "policy", "category", "target", "verified_by_object", "inferred_by_object", "target_effects", "duplicate_target_effects",
                                                   "other_object_effects", "accounting_agrees", "agent_claimed_success", "false_success_claim", "completed", "steps", "element_maps",
                                                   "seconds", "worker_error", "token_revoked", "spend_within_cap", "model_calls", "gateway_refusals", "model_calls_truncated")}
                            | {"spend_usd": (r.get("spend") or {}).get("cost_usd"), "isolation_gate_pass": (r.get("isolation_gate") or {}).get("pass")} for r in runs]}
    env = {"mode": C.MODE, "worker_sha256": B.sha256_file(GL.WORKER_V2), "images": {k: C.IMAGES[k] for k in ("browser", "worker")},
           "code_sha256": {f: B.sha256_file(C.ROOT / f) for f in ("afd/generic_llm.py", "afd/llm_episodes.py", "afd/generic.py", "afd/scoring_objects.py", "afd/faults/proxy.py", "afd/cdp_driver.py")},
           "services": {x["name"]: x["image"] for x in contract["services"]}, "config_sha256": cfg_sha, "contract_sha256": rep["contract_sha256"]}
    for name, obj in {"llm_config.json": cfg, "contract.json": K.redacted(contract), "protocol.json": protocol, "summary.json": summary, "environment.json": env}.items():
        (work / name).write_text(json.dumps(obj, indent=1, default=str))
    (work / "runs.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in runs))
    found = sorted({str(f.relative_to(work)) for f in work.rglob("*") if f.is_file() and f.suffix in (".json", ".jsonl", ".txt") for v in set(secrets) if v in f.read_text(errors="replace")})
    (work / "secret_scan.json").write_text(json.dumps({"secret_values_checked": len(set(secrets)), "found": len(found)}, indent=1))
    if found: raise Refused(f"secret scan FAILED in {found}; bundle left unsealed at {work}")
    digest = B.seal(work); final = OUT / bid; work.rename(final)
    return {"bundle": str(final), "digest": digest, "verifies": B.verify(final)["ok"], "preflight_pass": ok, "paid": paid, "spent_total_usd": round(total, 6),
            "preflight": {k: v["pass"] for k, v in pre.items()}, "episodes": summary["episodes"]}
