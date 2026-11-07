"""M2: real LLM browser episodes on Vikunja through the generic contract-driven proxy (config: studies/m2/vikunja.json).

Stages ($0 unless --paid):
  1. preflight (one job): self-test G1-G7 (contract-v1), containment probes from the worker's and the browser's network
     positions, gateway enforcement tests at $0 (budget refusal before any upstream call, bad/revoked token, model
     allow-list), key isolation (worker env names, no /run/secrets), the evidence-only worker run (element map, $0) and a
     scripted browser dry run (the harness stands in for the agent under lost_ack; per-object scoring).
  2. --paid: only if EVERY preflight check passes. One fresh job per policy: per-object scenario, lost_ack armed on the
     target, login bootstrap through CDP, one browser-use episode (glm-5.3-flash) with a per-episode token capped at
     episode_cap_usd; the next episode is refused if spent_total + episode_cap_usd > total_cap_usd.
Output: a sealed bundle in var/m2/ (MANIFEST.sha256), after a scan that every secret value is absent.
This is a smoke test of the real-agent path, NOT a qualification (n = 1 per policy)."""
import json, sys, time, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "studies" / "compat"))
from afd import bundle as B, config as C, contract as K, firewall as F, gateway as G, generic as GEN, generic_llm as GL, qualify as Q, scoring_objects as SO  # noqa: E402
import browser as BR  # noqa: E402  (the study's CDP driver; used only by the harness)

CFG_PATH = Path(sys.argv[sys.argv.index("--config") + 1]) if "--config" in sys.argv else ROOT / "studies" / "m2" / "vikunja.json"
CFG = json.loads((CFG_PATH if CFG_PATH.is_absolute() else ROOT / CFG_PATH).read_text())
# Strict copy of the study's CDP driver for the harness: "fill" fails if its element is missing (instead of typing into
# whatever has focus) and the driver stops at the first error. The study driver itself is unchanged.
_FILL = '''await js(f"(()=>{{const e=document.querySelector({json.dumps(st[1])}); e.focus(); e.select && e.select(); return true}})()")'''
_FILL_STRICT = '''ok_ = await js(f"(()=>{{const e=document.querySelector({json.dumps(st[1])}); if(!e) return false; e.focus(); e.select && e.select(); return document.activeElement===e}})()")
                    if not ok_: raise RuntimeError("fill target missing or not focused")'''
_ERR = '''            except Exception as e: rec["error"] = f"{type(e).__name__}: {e}"
            out.append(rec)'''
assert BR.DRIVER.count(_FILL) == 1 and BR.DRIVER.count(_ERR) == 1
DRIVER = BR.DRIVER.replace(_FILL, _FILL_STRICT).replace(_ERR, _ERR + "\n            if rec.get(\"error\") or rec.get(\"found\") is False: break")
OUT = ROOT / "var" / "m2"; OUT.mkdir(parents=True, exist_ok=True)

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
            "unclassified_write_list": base["unclassified_writes"], "object_records": recs, "other_writes": writes}   # outcome() replaces the list with its count

def login(job, s):
    """Fail-closed login bootstrap. Phase 1 only checks for the login form; credentials are typed (phase 2) ONLY into
    the form's own fields when it is present (the study driver types into whatever has focus if a selector is missing).
    If the form is absent, the browser must already be logged in (no login page); anything else aborts."""
    steps = K.render(CFG["login"], **s.vars)
    probe = job.drive(DRIVER, steps[:2])                                   # nav /login, wait for the form
    found = bool(probe and probe[-1].get("found"))
    url = lambda recs: (recs[-1].get("url") or "").split("?")[0].replace("http://afd-proxy:8080", "") if recs else None
    if not found:
        if url(probe) in (None, "", "/login"): raise RuntimeError("login bootstrap: no login form and not logged in; aborting")
        recs = job.drive(DRIVER, steps[-2:])                               # already logged in: go to the launch page only
        return {"phase2_typed_credentials": False, "already_logged_in": True, "errors": [r.get("error") for r in recs if r.get("error")], "final_url_path": url(recs)}
    recs = job.drive(DRIVER, steps)                                        # strict driver: stops if the form is missing
    if len(recs) != len(steps) or any(r.get("error") for r in recs): raise RuntimeError(f"login bootstrap failed at step {len(recs)}; aborting")
    after = url(recs)
    if after in (None, "/login"): raise RuntimeError("login bootstrap: still on the login page after submitting; aborting")
    return {"phase2_typed_credentials": True, "already_logged_in": False, "errors": [r.get("error") for r in recs if r.get("error")], "final_url_path": after}

def preflight(work):
    res, contract = {}, K.load(ROOT / CFG["contract"])
    with GL.LLMEpisodeJob(contract, "m2pre") as job:
        st = GEN.selftest_objects(job, 6); (work / "selftest.json").write_text(json.dumps(st, indent=1))
        check(res, "selftest_G1_G7", st["pass"] and st["complete"], {"checks": {k: v["pass"] for k, v in st["checks"].items()}})
        app = job.public["name"]; app_ip, app_port = job.ips[app], job.public["port"]
        proxy_agent_ip = GL.D.ip_of(job.proxy, job.nets["agent"])
        tw = {"app_direct": [app_ip, app_port], "proxy_app_side": [job.proxy_ip, 8080], "proxy_agent_side": [proxy_agent_ip, 8080],
              "internet_1.1.1.1_443": ["1.1.1.1", 443], "internet_8.8.8.8_53": ["8.8.8.8", 53], "gateway": [job.gw_ip, C.GATEWAY_PORT], "browser_cdp": [job.cdp_ip, 9222]}
        pw = job.probe([job.nets2["cdp"], job.nets2["gw"]], tw)
        check(res, "containment_worker_position", pw.get("tcp") and all(pw["tcp"][k].startswith("blocked") for k in ("app_direct", "proxy_app_side", "proxy_agent_side", "internet_1.1.1.1_443", "internet_8.8.8.8_53"))
              and pw["tcp"]["gateway"] == "OPEN" and pw["tcp"]["browser_cdp"] == "OPEN", {"networks": ["cdp", "gw"], **pw})
        tb = {"proxy": [GEN.PROXY_ALIAS, 8080], "app_direct": [app_ip, app_port], "gateway": [job.gw_ip, C.GATEWAY_PORT], "internet_1.1.1.1_443": ["1.1.1.1", 443]}
        pb = job.probe([job.nets["agent"], job.nets2["cdp"]], tb)
        check(res, "containment_browser_position", pb.get("tcp") and pb["tcp"]["proxy"] == "OPEN" and all(pb["tcp"][k].startswith("blocked") for k in ("app_direct", "gateway", "internet_1.1.1.1_443")),
              {"networks": ["agent", "cdp"], **pb})
        r = job.harness_container([job.nets["agent"]], ["-I", "-c", "import urllib.request as u\ntry: u.build_opener(u.ProxyHandler({})).open('http://afd-proxy:8080/_afd/evidence?scope=x', timeout=5); print('CTL:200')\nexcept Exception as e: print('CTL:' + str(getattr(e, 'code', type(e).__name__)))"], "probe")
        check(res, "proxy_control_api_needs_secret_from_agent_side", "CTL:403" in r.stdout, {"result": r.stdout.strip()[-40:]})
        # gateway enforcement at $0: a token whose budget is below any call's worst case is refused BEFORE an upstream call
        jid = f"{job.id}-gwtest"; tok, path = G.issue_token(jid, 0.000001)
        cases = {"budget_below_worst_case": [tok, C.MODEL], "model_not_allowed": [tok, "glm-5.3"], "bad_token": ["not-a-token", C.MODEL]}
        g = job.harness_container([job.nets2["gw"]], ["-I", "-c", GWTEST, job.gw_url, json.dumps(cases)], "probe", timeout=90)
        revoked = G.revoke(path)
        g2 = job.harness_container([job.nets2["gw"]], ["-I", "-c", GWTEST, job.gw_url, json.dumps({"revoked_token": [tok, C.MODEL]})], "probe", timeout=90)
        got = {**json.loads(g.stdout.split("GW:", 1)[1]), **json.loads(g2.stdout.split("GW:", 1)[1])} if "GW:" in g.stdout and "GW:" in g2.stdout else {"error": (g.stderr + g2.stderr)[-400:]}
        sp = G.job_spend(jid)
        check(res, "gateway_enforces_budget_at_zero_cost", got.get("budget_below_worst_case") == 402 and got.get("model_not_allowed") == 403 and got.get("bad_token") == 401
              and got.get("revoked_token") == 401 and revoked and sp["cost_usd"] == 0.0 and sp["budget_refusals"] >= 1, {"statuses": got, "ledger": sp, "token_revoked": revoked})
        names = sorted({"AFD_CDP_URL", "AFD_LAUNCH_URL", "AFD_ALLOWED_HOST", "AFD_INSTRUCTION", "AFD_POLICY", "AFD_MAX_STEPS", "AFD_GATEWAY_URL", "AFD_JOB_TOKEN", "AFD_MODEL"})
        pk = job.probe([job.nets2["cdp"], job.nets2["gw"]], {})
        check(res, "worker_has_no_provider_key", not any("VULTR" in n.upper() or n.upper().endswith("API_KEY") for n in names + pk.get("env_names", []))
              and pk.get("run_secrets_exists") is False and pk.get("uid") == C.WORKER_UID,
              {"worker_env_names": names, "probe_env_names": pk.get("env_names"), "run_secrets_exists": pk.get("run_secrets_exists"), "uid": pk.get("uid"),
               "key_mounted_only_in": C.GATEWAY_NAME})
        # evidence-only worker ($0): element map after the login bootstrap
        s = GEN.Scenario(job); lg = login(job, s)
        ev, _, secs = job.run_worker(CFG["launch_url"], evidence_only=True)
        els = (ev.get("elements_seen") or {}).get("elements") or []
        blob = json.dumps(ev); leaked = [i for i, v in enumerate(secrets_of(job, s)) if v in blob]
        check(res, "element_evidence_path", len(els) > 0 and all(set(e) <= {"index", "tag", "role", "type", "aria_label", "title", "placeholder", "name", "text"} for e in els) and not leaked,
              {"login": lg, "n_elements": len(els), "sample": els[:12], "secrets_found": len(leaked), "seconds": secs})
        # scripted dry run under lost_ack (the harness stands in for the agent)
        s = GEN.Scenario(job); job.arm(s.scope, CFG["condition"], 6, s.target); lg = login(job, s)
        recs = job.drive(DRIVER, K.render(CFG["dry_run_ui"], **s.vars)); time.sleep(3)
        o = score(job, s, {})
        check(res, "dry_run_lost_ack_through_ui", o["target_effects"] == 1 and o["accounting_agrees"] and o["other_object_effects"] == 0
              and any(r["intervention"] == "lost_ack" and r["outcome"] == "committed" for r in o["object_records"]),
              {"login": lg, "driver_errors": [r.get("error") for r in recs if r.get("error")],
               **{k: o[k] for k in ("target", "verified_by_object", "inferred_by_object", "attempts_by_object", "target_effects", "duplicate_target_effects", "other_object_effects", "accounting_agrees")},
               "object_records": [(r["object"], r["status_to_client"], r["intervention"], r["outcome"]) for r in o["object_records"]]})
    res["_teardown"] = job.teardown_report
    return res, all(v["pass"] for k, v in res.items() if not k.startswith("_")), secrets_of(job)

def worker_hash_checks(job):
    """The worker that will execute = the reviewed commit: host file, git blob at CFG['required_worker_commit'], and an
    in-container hash of the identical read-only mount."""
    import hashlib, subprocess
    host = B.sha256_file(GL.WORKER_V2)
    req = CFG.get("required_worker_commit")
    blob = subprocess.run(["git", "-C", str(ROOT), "show", f"{req}:afd/agents/browser_worker_v2.py"], capture_output=True).stdout if req else b""
    want = hashlib.sha256(blob).hexdigest() if blob else None
    r = job.harness_container([job.nets2["cdp"]], ["-I", "-c", "import hashlib; print('SHA:' + hashlib.sha256(open('/afd/browser_worker_v2.py','rb').read()).hexdigest())"],
                              "hash", mounts=[f"{GL.WORKER_V2}:/afd/browser_worker_v2.py:ro"], timeout=60)
    inside = r.stdout.split("SHA:", 1)[1].strip()[:64] if "SHA:" in r.stdout else None
    return {"required_commit": req, "commit_blob_sha256": want, "host_file_sha256": host, "in_container_sha256": inside,
            "match": bool(inside) and inside == host and (want is None or want == host)}

def diag_snapshot(job):
    """Independent read-only SQL view of rows (diagnostic only; never scored)."""
    d = CFG.get("diagnostic_snapshot")
    if not d: return None
    o = K.render(d, **job.vars)
    r = GL.D.sh("docker", "run", "--rm", "--network", job.nets["app"], *GL.D.labels(job.id, "oracle"), *GL.D.hardening("gateway", o.get("uid", 65534)),
                *[x for k, v in (o.get("env") or {}).items() for x in ("-e", f"{k}={v}")], "--entrypoint", o["entrypoint"], o["image"], *o["command"], check=False, timeout=120)
    if r.returncode != 0: return {"error": (r.stderr or "")[-300:]}
    return {"t": round(time.time(), 3), "rows": [l.split("\t") for l in r.stdout.splitlines() if l.strip()]}

def episode(work, policy, cap, idx):
    contract = K.load(ROOT / CFG["contract"])
    with GL.LLMEpisodeJob(contract, f"m2e{idx}") as job:
        s = GEN.Scenario(job); job.arm(s.scope, CFG["condition"], 6, s.target)
        lg = login(job, s)
        hash_before = worker_hash_checks(job)
        if not hash_before["match"]: raise SystemExit(f"worker hash check FAILED before the episode: {hash_before}; no token issued")
        diag_before = diag_snapshot(job)
        tok, path = G.issue_token(job.id, cap)
        t0 = time.time()
        try: result, shots, secs = job.run_worker(CFG["launch_url"], CFG["instruction"], Q.policy_text(policy), tok, CFG["max_steps"])
        finally: revoked = G.revoke(path)
        agent_end = time.time(); time.sleep(4)
        hash_after = worker_hash_checks(job); diag_after = diag_snapshot(job)
        spend = G.job_spend(job.id)
        lp = C.LEDGER_DIR / f"{job.id}.jsonl"
        usage = [json.loads(l) for l in lp.read_text().splitlines() if l.strip()] if lp.exists() else []
        claim = result.get("claim") or {}
        cat = "budget_stop" if spend.get("budget_refusals") else ("agent" if "claim" in result and not result.get("error") else ("agent" if claim else "infrastructure"))
        o = score(job, s, claim)
        rec = {"run_id": f"e{idx}", "policy": policy, "condition": CFG["condition"], "role": "positive_control" if policy == Q.POSITIVE_CONTROL else "candidate",
               "category": cat, **o, "proxy_scope_requests": job.evidence(s.scope)["requests"], "claim": claim, "worker_error": result.get("error"),
               "steps": len(result.get("steps") or []), "seconds": round(agent_end - t0, 1), "login": lg, "spend": spend, "token_revoked": revoked,
               "episode_cap_usd": cap, "model": C.MODEL, "worker_hash_before": hash_before, "worker_hash_after": hash_after,
               "worker_result_present": "claim" in result or bool(result.get("steps")), "worker_stderr_tail": result.get("stderr_tail"),
               "export_ok": not (result.get("error") or "").count("export failed") and result.get("error") != "no result from worker",
               "model_calls": len(usage), "model_calls_truncated": sum(u.get("finish_reason") == "length" for u in usage)}
        ed = work / "episodes" / f"e{idx}"; (ed / "screenshots").mkdir(parents=True)
        (ed / "result.json").write_text(json.dumps(result, indent=1, default=str))
        (ed / "model_usage.jsonl").write_text("".join(json.dumps(u) + "\n" for u in usage))
        if diag_before is not None: (ed / "diagnostic_rows.json").write_text(json.dumps({"about": CFG["diagnostic_snapshot"]["about"], "before": diag_before, "after": diag_after}, indent=1))
        for name, data in shots.items(): (ed / name).write_bytes(data)
        sec = secrets_of(job, s, [tok])
    rec["teardown"] = job.teardown_report
    return rec, sec

def main():
    paid = "--paid" in sys.argv
    bid = f"{CFG.get('bundle_prefix', 'm2')}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    work = OUT / f".{bid}.partial"; work.mkdir(parents=True)
    F.install(); G.up()
    pre, ok, secrets = preflight(work)
    (work / "preflight.json").write_text(json.dumps(pre, indent=1, default=str))
    print("PREFLIGHT", "PASS" if ok else "FAIL", {k: v["pass"] for k, v in pre.items() if not k.startswith("_")}, flush=True)
    runs, total = [], 0.0
    if paid and not ok: print("paid stage NOT started: preflight failed", flush=True)
    if paid and ok:
        for i, pol in enumerate(CFG["policies"], 1):
            if total + CFG["episode_cap_usd"] > CFG["total_cap_usd"] + 1e-12:
                runs.append({"run_id": f"e{i}", "policy": pol, "category": "refused_total_cap", "spent_total_before": total}); continue
            rec, sec = episode(work, pol, CFG["episode_cap_usd"], i); secrets += sec
            total += rec["spend"]["cost_usd"]; runs.append(rec)
            print("EPISODE", i, pol, {k: rec.get(k) for k in ("category", "target_effects", "duplicate_target_effects", "other_object_effects", "accounting_agrees",
                  "agent_claimed_success", "false_success_claim", "steps", "worker_error")}, "spend", rec["spend"]["cost_usd"], flush=True)
    contract = K.load(ROOT / CFG["contract"]); protocol = K.protocol_for(contract)
    summary = {"bundle": bid, "kind": "M2 smoke: real LLM browser episodes through the generic proxy (n = 1 per policy; NOT a qualification)",
               "contract": contract["name"], "protocol": protocol["protocol"], "paid": paid, "preflight_pass": ok, "model": C.MODEL,
               "caps": {"episode_usd": CFG["episode_cap_usd"], "total_usd": CFG["total_cap_usd"]}, "spent_total_usd": round(total, 6),
               "episodes": [{k: r.get(k) for k in ("run_id", "policy", "role", "category", "target", "verified_by_object", "inferred_by_object", "attempts_by_object",
                                                   "target_effects", "duplicate_target_effects", "other_object_effects", "unclassified_writes", "accounting_agrees",
                                                   "agent_claimed_success", "false_success_claim", "completed", "steps", "seconds", "worker_error", "token_revoked",
                                                   "export_ok", "worker_result_present", "model_calls", "model_calls_truncated")}
                            | {"worker_hash_match": bool((r.get("worker_hash_before") or {}).get("match") and (r.get("worker_hash_after") or {}).get("match")),
                               "executed_worker_sha256": (r.get("worker_hash_before") or {}).get("in_container_sha256")}
                            | {"spend_usd": (r.get("spend") or {}).get("cost_usd"), "calls": (r.get("spend") or {}).get("calls")} for r in runs]}
    for name, obj in {"m2_config.json": CFG, "contract.json": K.redacted(contract), "protocol.json": protocol, "summary.json": summary,
                      "environment.json": {"worker": "afd/agents/browser_worker_v2.py", "worker_sha256": B.sha256_file(GL.WORKER_V2), "worker_image": C.IMAGES["worker"],
                                           "browser_image": C.IMAGES["browser"], "proxy_sha256": B.sha256_file(GEN.PROXY_SRC),
                                           "scoring_objects_sha256": B.sha256_file(ROOT / "afd" / "scoring_objects.py"), "generic_llm_sha256": B.sha256_file(ROOT / "afd" / "generic_llm.py"),
                                           "services": {s["name"]: s["image"] for s in contract["services"]}}}.items():
        (work / name).write_text(json.dumps(obj, indent=1, default=str))
    (work / "runs.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in runs))
    # fail closed: no secret value may appear anywhere in the bundle
    found = [(str(f.relative_to(work)), i) for f in work.rglob("*") if f.is_file() and f.suffix in (".json", ".jsonl", ".txt") for i, v in enumerate(set(secrets)) if v in f.read_text(errors="replace")]
    (work / "secret_scan.json").write_text(json.dumps({"secret_values_checked": len(set(secrets)), "found": len(found)}, indent=1))
    if found: raise SystemExit(f"secret scan FAILED in {[f for f, _ in found]}; bundle left unsealed at {work}")
    digest = B.seal(work); final = OUT / bid; work.rename(final)
    print("BUNDLE", final, digest, "verifies", B.verify(final)["ok"], "spent_total_usd", round(total, 6), flush=True)

if __name__ == "__main__":
    main()
