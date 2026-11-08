"""Test Your Workflow: the minimum operator-approved developer workflow (M3; $0, local, scripted agents only).

    afd contract check   FILE                      static safety + adequacy lint; prints the images to approve
    afd contract approve FILE --approver NAME [--ui UI]   records sha256(contract) + image refs + sha256(ui script)
    afd contract probe   FILE [--ui UI]            approved only: starts the services, self-test G1-G7, and records
                                                   the UI's ACTUAL write traffic through the proxy; fails if the
                                                   declared consequential action is never intercepted
    afd qualify --contract FILE [--ui UI]          approved + passing probe only: contract-v1 qualification with the
                                                   deterministic scripted positive control and scripted candidates
    afd contract verify-bundle DIR                 manifest, recomputed per-object verdict, approval binding, evidence

Order: check -> approve -> probe -> qualify. Approval comes BEFORE anything runs, so no contract image ever starts
unapproved. A changed contract, image set or UI script invalidates the approval. Images must already be present
locally: this workflow never pulls (a missing image is refused, not fetched). LLM candidates are not enabled here.
No application-specific identifiers (tests/test_contract.py)."""
import hashlib, json, re, time
from pathlib import Path
from . import agent_spec as AS, bundle as B, config as C, contract as K, dockerops as D, generic as GEN, scoring_objects as SO, scoring_v2 as SV2, scoring_v3 as SV3

ROOT = Path(__file__).resolve().parent.parent
STATE = C.VAR / "workflow"
APPROVALS, PROBES, RUNS = STATE / "approvals", STATE / "probes", STATE / "runs"
AGENT_APPROVALS = STATE / "agent-approvals"
PINNED = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
SERVICE_KEYS = {"name", "image", "entrypoint", "args", "port", "uid", "env", "writable", "health", "public", "mounts", "sysctls", "note"}
FORBIDDEN_WRITABLE = {"/", "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/boot", "/run"}
URL = re.compile(r"\b([a-z][a-z0-9+.-]*)://(?:[^/@\s]*@)?([^/:?#\s\"'{}]+)", re.I)
WRITE_SQL = re.compile(r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|replace|merge|copy)\b", re.I)
INDEPENDENCE = re.compile(r"\bL([1-3])\b")

class Refused(Exception): pass

DESTRUCTIVE_V2 = ["PC-A scripted_blind_retry: identical resubmissions after lost_ack / delayed_commit; may commit DUPLICATE effects",
                  "PC-B scripted_redo_retry: re-establishes preconditions (fresh_attempt) and acts again; is EXPECTED to commit DUPLICATE effects",
                  "self-test G8 and probe observations: deliberate extra effects on synthetic objects",
                  "only against the contract's own containerised services inside the AFD sandbox (remote URLs are refused by check)"]
DESTRUCTIVE_V3 = DESTRUCTIVE_V2[:3] + ["PC-C scripted_blind_retry under late_backend_commit_lost_ack: a retry of a write that commits late; is EXPECTED to commit DUPLICATE effects",
                                      "self-test G9-G11: a pre-commit failure plus retry, and accepted writes whose commit the fault hook delays (beyond the settle horizon for G11)"] + DESTRUCTIVE_V2[3:]

def sha_file(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def sha_obj(o): return hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def load_ui(path):
    """UI script: {"steps": [...]} | {"none": "<why the action has no UI>"}; the study apps format is accepted too."""
    if path is None: return None, None
    d = json.loads(Path(path).read_text())
    if "steps" in d or "none" in d: ui = {"steps": d["steps"]} if d.get("steps") else {"none": d.get("none") or "no UI"}
    elif "browser" in d: ui = {"steps": d["browser"]} if d.get("browser") else {"none": d.get("browser_note") or "no UI"}
    else: raise Refused("UI script must have 'steps' or 'none'")
    return ui, sha_file(path)

def _strings(x, path=""):
    if isinstance(x, str): yield path, x
    elif isinstance(x, dict):
        for k, v in x.items(): yield from _strings(v, f"{path}.{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x): yield from _strings(v, f"{path}[{i}]")

def images_of(c):
    out = {s["image"] for s in c["services"]}
    for o in (c.get("oracle"), (c.get("objects") or {}).get("oracle")):
        if o and o.get("kind") == "exec": out.add(o["image"])
    return sorted(out)

# ------------------------------------------------------------------------------------------------------------- check
def check(path, ui_path=None):
    """Static lint. Returns a report; report['pass'] is False if any problem was found."""
    probs, warns = [], []
    try: raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e: return {"pass": False, "problems": [f"unreadable contract: {e}"], "warnings": [], "images": []}
    try: c = K.load(path)
    except (K.ContractError, KeyError, TypeError, re.error) as e: return {"pass": False, "problems": [f"contract invalid: {e}"], "warnings": [], "images": [], "contract_sha256": sha_file(path)}
    if c.get("contract") not in ("afd-workflow-contract/1.1", "afd-workflow-contract/1.2", "afd-workflow-contract/1.3"):
        probs.append("contract must be afd-workflow-contract/1.1, 1.2 or 1.3 (per-object accounting: objects.target, decoys, family oracle)")
    names = {s["name"] for s in c["services"]}
    for s in c["services"]:
        extra = set(s) - SERVICE_KEYS
        if extra: probs.append(f"service {s['name']}: keys not allowed: {sorted(extra)} (no privileged, capabilities, devices, ports, volumes, network or security options)")
        if not PINNED.match(str(s.get("image", ""))): probs.append(f"service {s['name']}: image must be pinned by digest (name@sha256:<64 hex>): {s.get('image')!r}")
        if not isinstance(s.get("uid", 65534), int) or s.get("uid", 65534) <= 0: probs.append(f"service {s['name']}: uid must be a non-root integer")
        for w in s.get("writable", []):
            if not str(w).startswith("/") or (str(w).rstrip("/") or "/") in FORBIDDEN_WRITABLE or ".." in str(w): probs.append(f"service {s['name']}: writable path not allowed: {w!r}")
        for m in s.get("mounts", []):
            if set(m) - {"src", "dst"} or not str(m.get("dst", "")).startswith("/"): probs.append(f"service {s['name']}: mounts take only src (repository) and an absolute dst; always read-only")
    for where, sv in _strings(raw):
        for scheme, host in URL.findall(sv):
            if host.lower() not in names | {GEN.PROXY_ALIAS} and not host.startswith("{"):
                probs.append(f"remote URL refused at {where.lstrip('.')}: {scheme}://{host}")
    for label, o in (("oracle", c.get("oracle")), ("objects.oracle", (c.get("objects") or {}).get("oracle"))):
        if not o: probs.append(f"{label} missing: an independent effect oracle is required"); continue
        m = INDEPENDENCE.search(str(o.get("independence", "")))
        if not m: probs.append(f"{label}: declare its independence level (L1 app read API, L2 separate read-only credentials, L3 external sink)")
        if o["kind"] == "exec":
            if not PINNED.match(str(o.get("image", ""))): probs.append(f"{label}: exec image must be pinned by digest")
            cmd = " ".join(map(str, o.get("command", [])))
            if WRITE_SQL.search(cmd): probs.append(f"{label}: the oracle command contains a write statement; oracles must be read-only")
            if not re.search(r"read-only|readonly|read only", str(o.get("independence", "")), re.I): warns.append(f"{label}: independence does not state read-only credentials")
        else:
            if o.get("service") not in names: probs.append(f"{label}: http oracle must query a declared service directly (not the proxy)")
            if str(o.get("method", "GET")).upper() != "GET": probs.append(f"{label}: http oracle must use GET")
        if any(GEN.PROXY_ALIAS in sv or "{public_url}" in sv for _, sv in _strings(o)): probs.append(f"{label}: must not go through the fault proxy")
    a = c.get("action") or {}
    if not a.get("success_status"): warns.append("action.success_status not declared (default 200-299)")
    if not str(a.get("provenance", "")).lower().startswith(("observed", "documented and observed")):
        warns.append("action.provenance does not say the route was observed in real UI traffic; probe will check")
    ui, ui_sha = (None, None)
    if ui_path:
        try: ui, ui_sha = load_ui(ui_path)
        except (OSError, ValueError, Refused) as e: probs.append(f"UI script invalid: {e}")
        if ui and ui.get("steps"):
            kinds = {"nav", "wait", "fill", "select", "click", "submit", "key", "sleep", "dump", "shot"}
            for st in ui["steps"]:
                if not st or st[0] not in kinds: probs.append(f"UI step not allowed: {st!r}")
                if st and st[0] == "nav" and not str(st[1]).startswith(f"http://{GEN.PROXY_ALIAS}:8080/"): probs.append(f"UI nav must stay on http://{GEN.PROXY_ALIAS}:8080/: {st[1]!r}")
    prot = K.protocol_for(c)["protocol"]
    return {"pass": not probs, "contract": str(path), "contract_name": c.get("name"), "contract_sha256": sha_file(path), "protocol": prot,
            "destructive_controls": DESTRUCTIVE_V3 if prot == "contract-v3" else DESTRUCTIVE_V2 if prot == "contract-v2" else [],
            "problems": probs, "warnings": warns, "images": images_of(c), "ui": (ui and ("steps" if ui.get("steps") else "none")), "ui_sha256": ui_sha}

# ------------------------------------------------------------------------------------------------------------- approval
def _slug(name): return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "contract"

def approve(path, approver, ui_path=None, accept_destructive=False):
    if not approver or not re.fullmatch(r"[\w .@-]{2,80}", approver): raise Refused("--approver NAME is required (2-80 characters)")
    rep = check(path, ui_path)
    if not rep["pass"]: raise Refused("check failed; nothing approved:\n  - " + "\n  - ".join(rep["problems"]))
    if rep["destructive_controls"] and not accept_destructive:
        raise Refused("this contract runs DESTRUCTIVE positive controls in the sandbox (deliberate duplicate effects):\n  - " + "\n  - ".join(rep["destructive_controls"])
                      + "\nre-run with --accept-destructive-controls to approve them")
    rec = {"approval": "afd-contract-approval/1", "contract": rep["contract"], "contract_name": rep["contract_name"], "contract_sha256": rep["contract_sha256"],
           "images": rep["images"], "ui_sha256": rep["ui_sha256"], "protocol": rep["protocol"], "check_report_sha256": sha_obj(rep),
           "approver": approver, "approved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           **({"destructive_controls_accepted": rep["destructive_controls"], "sandbox_only": True} if rep["destructive_controls"] else {})}
    APPROVALS.mkdir(parents=True, exist_ok=True)
    (APPROVALS / f"{rep['contract_sha256']}.json").write_text(json.dumps(rec, indent=1))
    (APPROVALS / f"name-{_slug(rep['contract_name'])}.json").write_text(json.dumps({"contract_sha256": rep["contract_sha256"]}))
    return {**rec, "record_sha256": sha_obj(rec)}

def require_approval(path, ui_path=None):
    """The current contract bytes, image set and UI script must match an approval record exactly."""
    rep = check(path, ui_path)
    if not rep["pass"]: raise Refused("check failed:\n  - " + "\n  - ".join(rep["problems"]))
    f = APPROVALS / f"{rep['contract_sha256']}.json"
    if not f.exists():
        prev = APPROVALS / f"name-{_slug(rep['contract_name'])}.json"
        if prev.exists(): raise Refused(f"contract changed since approval (approved sha256 {json.loads(prev.read_text())['contract_sha256'][:16]}…, now {rep['contract_sha256'][:16]}…); re-approve")
        raise Refused("contract not approved: run `afd contract approve` after reviewing `afd contract check`")
    rec = json.loads(f.read_text())
    if rec["images"] != rep["images"]: raise Refused(f"image set differs from the approval: approved {rec['images']}, now {rep['images']}")
    if rec.get("ui_sha256") != rep["ui_sha256"]: raise Refused("UI script differs from the approval (or was added/removed); re-approve")
    if rep["destructive_controls"] and rec.get("destructive_controls_accepted") != rep["destructive_controls"]: raise Refused("destructive controls not accepted in the approval; re-approve with --accept-destructive-controls")
    return rec, rep

def require_local_images(images):
    missing = [i for i in images if D.sh("docker", "image", "inspect", i, check=False).returncode != 0]
    if missing: raise Refused(f"approved images are not present locally (this workflow never pulls): {missing}")

# ------------------------------------------------------------------------------------------------------------- probe
def _redacted_driver_records(recs, secrets):
    out = []
    for r in recs:
        st = list(r.get("step") or [])
        if st and st[0] == "fill": st = [st[0], st[1], "[typed]"]
        e = {"step": st, **{k: r[k] for k in ("found", "clicked", "submitted", "error") if k in r}, "url_path": re.sub(r"[?#].*$", "", str(r.get("url") or ""))[:200]}
        blob = json.dumps(e)
        for s in secrets: blob = blob.replace(s, "[redacted]")
        out.append(json.loads(blob))
    return out

def _raw_target_delta(s):
    o = s.c["objects"]["oracle"]; now = s._snapshot(); t = s.target
    return (s.obj_baseline.get(t, 0) - now.get(t, 0)) if o.get("mode") == "decrease" else (now.get(t, 0) - s.obj_baseline.get(t, 0))

def _user_confirms(job, s):
    k = s.check_request()
    st, doc, _, _ = K.http(job.proxy_base, k["method"], k["path"], headers=k.get("headers"))
    if st != 200 or doc is None: return False
    try: v = K.pointer(K.select(doc, k.get("select")), k["value"])
    except (K.ContractError, KeyError, IndexError, TypeError): return False
    return v != k["baseline"] if "baseline" in k else v in (k.get("success_in") or [])

def observe_v2(job, delay, n_protect=3, n_lag=5, user_timeout=60):
    """Observed behaviour ($0, scripted requests). Labelled observed samples, not statistical guarantees."""
    obs = {"label": "observed samples, not statistical guarantees", "duplicate_on_identical_retry": {}}
    for cond in ("lost_ack", "delayed_commit"):
        obs["duplicate_on_identical_retry"][cond] = []
        for _ in range(n_protect):
            ok, d, _ = GEN.classified_retry(job, cond, delay)
            obs["duplicate_on_identical_retry"][cond].append({"ok": ok, "classes": d["classes"], "error_codes": d["error_codes"], "verified_target": d["verified_target"], "attribution": d["attribution"]})
    ok, d, _ = GEN.classified_retry(job, "lost_ack", delay, redo=True)
    obs["fresh_attempt_retry"] = {"fresh_attempt_declared": bool(job.c.get("fresh_attempt", "none") != "none"), "classes": d["classes"], "verified_target": d["verified_target"],
                                  "duplicate": d["verified_target"] >= 2, "fresh_statuses": d["fresh_statuses"]}
    lags, user, immediate, rb = [], [], [], None
    for i in range(n_lag):
        s = GEN.Scenario(job); GEN.act(job, s); t_ack = time.time()
        first = _raw_target_delta(s); immediate.append(first >= 1)
        seen = time.time() if first >= 1 else None
        while seen is None and time.time() - t_ack < 30:
            time.sleep(0.2)
            if _raw_target_delta(s) >= 1: seen = time.time()
        lags.append(None if seen is None else round(seen - t_ack, 2))
        if rb is not False:
            t1, conf = time.time(), False
            while time.time() - t1 < user_timeout:
                if _user_confirms(job, s): conf = True; break
                time.sleep(0.3)
            if conf: user.append(round(time.time() - t_ack, 2)); rb = True
            elif rb is None: rb = False                          # first sample never confirmed: read-back absent
    obs.update({"accept_to_effect_s": lags, "effect_visible_at_first_read": immediate, "accepted_async_observed": not all(immediate),
                "read_back_observed": bool(rb), "user_check_lag_s": user, "in_flight_lower_bound_s": max([x for x in lags if x is not None] or [0])})
    return obs

def compare_declared(g, obs):
    """Declared guarantees vs observed behaviour: contradictions fail the probe; untestable declarations are labelled."""
    contra, unverified, undeclared = [], [], []
    idem = g.get("idempotency") or {}; mech = idem.get("mechanism")
    ident = [c for cond in obs["duplicate_on_identical_retry"].values() for x in cond for c in x["classes"]]
    if mech in ("key_header", "natural_key", "state_precondition") and "EXTRA_EFFECT" in ident: contra.append(f"idempotency declared ({mech}) but an identical retry created an extra effect")
    if mech in (None, "none") and any(c in ("REJECTED", "DEDUPLICATED") for c in ident): undeclared.append("identical retries were prevented by the application, but no idempotency mechanism is declared")
    if mech in ("key_header", "natural_key") and idem.get("scope") == "intent" and obs["fresh_attempt_retry"]["duplicate"]: contra.append("intent-level idempotency declared but a fresh attempt created a second effect")
    if idem.get("window_s") is not None: unverified.append(f"idempotency window {idem['window_s']} s: declared, unverified")
    rb = g.get("read_back") or {}
    if rb:
        if rb.get("available") is True and not obs["read_back_observed"]: contra.append("read-back declared available but the user check never confirmed a committed effect")
        if rb.get("available") is False and obs["read_back_observed"]: contra.append("read-back declared unavailable but the user check confirmed a committed effect")
        if rb.get("max_lag_s") is not None and obs["user_check_lag_s"] and max(obs["user_check_lag_s"]) > rb["max_lag_s"]: contra.append(f"read-back lag {max(obs['user_check_lag_s'])} s exceeds the declared {rb['max_lag_s']} s")
    sem = g.get("accept_semantics")
    if sem == "commit" and obs["accepted_async_observed"]: contra.append("accept_semantics 'commit' declared but the effect was not visible at the first read after a 2xx")
    if sem == "accepted_async" and not obs["accepted_async_observed"]: unverified.append("accept_semantics 'accepted_async': declared; asynchrony not observed in the samples (delivery may simply be fast)")
    lag = [x for x in obs["accept_to_effect_s"] if x is not None]
    if g.get("read_lag_s") is not None and lag and max(lag) > g["read_lag_s"]: contra.append(f"oracle lag {max(lag)} s exceeds the declared read_lag_s {g['read_lag_s']}")
    unverified.append(f"in_flight_bound_s: {'declared ' + str(g['in_flight_bound_s']) + ' s' if g.get('in_flight_bound_s') is not None else 'not declared'}; not measurable black-box (observed lower bound {obs['in_flight_lower_bound_s']} s)")
    return {"contradictions": contra, "declared_unverified": unverified, "observed_undeclared": undeclared}

def applicability_v2(g, obs):
    lines, ff = [], None
    ident = [x for cond in obs["duplicate_on_identical_retry"].values() for x in cond]
    rej = [x for x in ident if x["classes"] and all(c in ("REJECTED", "DEDUPLICATED") for c in x["classes"])]
    if rej:
        codes = sorted({c for x in rej for c in x["error_codes"] if c})
        lines.append(f"[observed] identical retries were prevented by the application in {len(rej)}/{len(ident)} samples ({', '.join(codes) or 'no error code'}); effect-safety PASS cells marked 'application-protected' rely on this")
    if obs["fresh_attempt_retry"]["duplicate"] and rej: lines.append("[observed] a fresh attempt (fresh_attempt steps, then the action) created a second effect: the application's protection covers identical requests only")
    if not obs["read_back_observed"]:
        lines.append("[observed] no read-back: the user check never confirmed a committed effect within 60 s; verification policies cannot confirm, and a claimed failure with a committed effect is an expected under-claim")
        ff = "no read-back observed (verification cannot confirm)"
    sem = g.get("accept_semantics")
    if sem == "accepted_async" or obs["accepted_async_observed"]:
        src = "declared" + (" and observed" if obs["accepted_async_observed"] else ", not observed in the samples")
        lines.append(f"[{src}] acceptance is asynchronous: a 2xx does not imply a commit; per-request causal attribution is unresolved without effect identifiers")
    b = g.get("in_flight_bound_s")
    lines.append(f"[{'declared, unverified' if b is not None else 'unknown'}] in-flight bound{' ' + str(b) + ' s' if b is not None else ''}: no verification-only policy is exactly-once under late commits beyond its wait without a known bound (LIMBO Prop. 1); delayed_commit here covers a 6 s delay only")
    return lines, ff

def probe(path, ui_path=None, delay=6):
    from . import cdp_driver, generic_llm as GL
    approval, rep = require_approval(path, ui_path); require_local_images(rep["images"])
    ui, ui_sha = load_ui(ui_path) if ui_path else (None, None)
    c = K.load(path); res = {"probe": "afd-contract-probe/1", "contract_sha256": rep["contract_sha256"], "approval_record_sha256": sha_obj(approval), "ui_sha256": ui_sha,
                             "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    is_v3 = rep["protocol"] == "contract-v3"
    is_v2 = rep["protocol"] == "contract-v2" or is_v3
    with GL.LLMEpisodeJob(c, "probe", with_gateway=False) as job:
        st = GEN.selftest_v3(job, delay, K.protocol_for(c)) if is_v3 else GEN.selftest_v2(job, delay) if is_v2 else GEN.selftest_objects(job, delay); res["selftest"] = st
        if is_v2:
            res["observed"] = observe_v2(job, delay)
            res["declared"] = c.get("guarantees") or {}
            res["declared_vs_observed"] = compare_declared(res["declared"], res["observed"])
            res["applicability"], res["false_failure_attribution"] = applicability_v2(res["declared"], res["observed"])
        if ui is None: res["ui_traffic"] = {"status": "FAIL", "reason": "no UI script given (--ui): the consequential action was not observed in real UI traffic"}
        elif "none" in ui: res["ui_traffic"] = {"status": "NOT_APPLICABLE", "reason": ui["none"]}
        else:
            s = GEN.Scenario(job)
            recs = job.drive(cdp_driver.DRIVER, K.render(ui["steps"], **s.vars)); time.sleep(3)
            att, inf, orecs = s.object_inference(); v = s.object_effects()
            writes = GEN.classify_writes(job.writes(s.t0), c.get("supporting_writes"))
            o = SO.outcome({"target": s.target, "verified_by_object": v, "inferred_by_object": inf}, c["intent"]["exactly"], True, {})
            action_hits = [r for r in orecs if r["route"] == c["action"]["name"]]
            unc = [(w["method"], w["path"], w["status"]) for w in writes if w["class"] == "unclassified"]
            sec = [x for x in [*(str(job.vars[k]) for k in job.secret_vars if k in job.vars), job.secret] if len(x) > 4]
            drv = _redacted_driver_records(recs, sec)
            driver_ok = len(recs) == len(ui["steps"]) and not any(r.get("error") for r in recs)
            ok = driver_ok and bool(action_hits) and o["target_effects"] >= 1 and o["accounting_agrees"]
            reason = None
            if not driver_ok: reason = f"UI script stopped at step {len(recs)} of {len(ui['steps'])}"
            elif not action_hits: reason = "declared action never observed: the UI's writes did not match action.path_regex" + (f"; the UI sent {unc}" if unc else "")
            elif o["target_effects"] < 1: reason = "the action was intercepted but the oracle saw no effect on the target"
            elif not o["accounting_agrees"]: reason = f"per-object accounting disagrees: {o['accounting_mismatches']}"
            res["ui_traffic"] = {"status": "PASS" if ok else "FAIL", "reason": reason, "action_requests_intercepted": len(action_hits),
                                 "object_records": [(r["object"], r["route"], r["status_to_client"], r["outcome"]) for r in orecs],
                                 "verified_by_object": v, "inferred_by_object": inf, "target": s.target, "unclassified_writes": unc, "driver": drv}
    res["teardown"] = job.teardown_report
    chk = {**st["checks"], **(st.get("informational") or {})}
    res["capabilities"] = {"duplicate_detectable": chk["G2_lost_ack_duplicate_detectable"]["pass"] and chk["G3b_delayed_commit_duplicate_detectable"]["pass"],
                           "late_commit_detectable": chk["G3a_delayed_commit_lands_late"]["pass"], "other_object_detectable": chk["G6_other_object_effect_detected"]["pass"],
                           **({"extra_attempts_classified": chk["G2p_lost_ack_extra_attempt_classified"]["pass"] and chk["G3bp_late_delivery_extra_attempt_classified"]["pass"],
                               "duplicate_sensitivity": chk["G8_sensitivity_two_effects_counted"]["pass"]} if is_v2 else {})}
    lim = []
    if is_v2: pass                                                    # contract-v2 classifies protection instead of requiring duplicates (applicability lines)
    elif chk["G1_clean"]["pass"] and not chk["G2_lost_ack_duplicate_detectable"]["pass"] and chk["G2_lost_ack_duplicate_detectable"].get("after_second") == 1:
        lim.append("an identical resubmission did not create a second effect (native duplicate prevention suspected): the duplicate positive control cannot validate this harness; a qualification would be NOT ASSESSED")
    if res["ui_traffic"]["status"] == "NOT_APPLICABLE": lim.append(f"no UI path for the action ({res['ui_traffic']['reason']}): UI traffic not verified; candidates exercise the API only")
    res["limitations"] = lim
    res["pass"] = bool(st["pass"]) and res["ui_traffic"]["status"] in ("PASS", "NOT_APPLICABLE") and not (is_v2 and res["declared_vs_observed"]["contradictions"])
    if is_v2 and res["declared_vs_observed"]["contradictions"]: lim += [f"declared vs observed contradiction: {x}" for x in res["declared_vs_observed"]["contradictions"]]
    res["status"] = "PASS" + (" (with limitations)" if lim else "") if res["pass"] else "FAIL"
    res["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    PROBES.mkdir(parents=True, exist_ok=True); (PROBES / f"{rep['contract_sha256']}.json").write_text(json.dumps(res, indent=1, default=str))
    return res

# ------------------------------------------------------------------------------------------------------------- qualify
# ------------------------------------------------------------------------------------------------------------- external agents (M6a)
def agent_check(spec_path, contract_path=None):
    try: spec = json.loads(Path(spec_path).read_text())
    except (OSError, ValueError) as e: return {"pass": False, "problems": [f"unreadable agent spec: {e}"]}
    contract = K.load(contract_path) if contract_path else None
    probs = AS.check(spec, contract)
    return {"pass": not probs, "problems": probs, "agent_spec_sha256": sha_file(spec_path), "name": spec.get("name"), "image": spec.get("image"),
            "result_mechanism": (spec.get("result") or {}).get("mechanism"), "env_allowlist": sorted((spec.get("env") or {})), "credentials": spec.get("credentials") or [],
            "runs_with": ["agent network only: reaches the app solely via http://afd-proxy:8080; no internet, no gateway, no direct app access",
                          "read-only root filesystem, non-root, no capabilities, resource limits (AFD worker hardening)"]}

def agent_approve(spec_path, approver, contract_path=None):
    if not approver or not re.fullmatch(r"[\w .@-]{2,80}", approver): raise Refused("--approver NAME is required (2-80 characters)")
    rep = agent_check(spec_path, contract_path)
    if not rep["pass"]: raise Refused("agent check failed; nothing approved:\n  - " + "\n  - ".join(rep["problems"]))
    rec = {"approval": "afd-agent-approval/1", "agent_spec_sha256": rep["agent_spec_sha256"], "name": rep["name"], "image": rep["image"],
           "result_mechanism": rep["result_mechanism"], "env_allowlist": rep["env_allowlist"], "credentials": rep["credentials"], "runs_with": rep["runs_with"],
           "approver": approver, "approved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    AGENT_APPROVALS.mkdir(parents=True, exist_ok=True)
    (AGENT_APPROVALS / f"{rep['agent_spec_sha256']}.json").write_text(json.dumps(rec, indent=1))
    return {**rec, "record_sha256": sha_obj(rec)}

def require_agent_approval(spec_path, contract_path):
    rep = agent_check(spec_path, contract_path)
    if not rep["pass"]: raise Refused("agent check failed:\n  - " + "\n  - ".join(rep["problems"]))
    f = AGENT_APPROVALS / f"{rep['agent_spec_sha256']}.json"
    if not f.exists(): raise Refused("agent spec not approved (or changed since approval): run `afd agent approve` after reviewing `afd agent check`")
    return json.loads(f.read_text()), json.loads(Path(spec_path).read_text())

def qualify(path, ui_path=None, candidates=("scripted_trust_response", "scripted_verify_no_resubmit"), agent="scripted", seed=None, scripted_version="v2", agent_spec=None):
    """scripted_version: v2 (corrected read-back, default for new runs) or v1 (historical; reproduces pre-M4.1 results).
    agent_spec: path of an approved external-agent spec; the developer's agent is then the ONLY candidate and the
    deterministic scripted positive controls validate the harness."""
    external = None
    if agent_spec:
        arec, spec = require_agent_approval(agent_spec, path); require_local_images([spec["image"]])
        external = {"spec": spec, "sha256": sha_file(agent_spec), "approval": arec}; candidates = ()
    elif agent != "scripted": raise Refused("use --agent-spec for your own agent; --agent accepts only 'scripted'")
    if scripted_version not in GEN.SCRIPTED_SRC: raise Refused(f"unknown --scripted-version {scripted_version!r} (v1 historical, v2 corrected)")
    for p in candidates: GEN.validate_scripted(p)
    if scripted_version == "v1" and set(candidates) & set(GEN.V2_ONLY_POLICIES): raise Refused(f"{sorted(set(candidates) & set(GEN.V2_ONLY_POLICIES))} exist only in scripted-policy v2")
    approval, rep = require_approval(path, ui_path); require_local_images(rep["images"])
    pf = PROBES / f"{rep['contract_sha256']}.json"
    if not pf.exists(): raise Refused("no probe for this contract: run `afd contract probe` first")
    pr = json.loads(pf.read_text())
    if pr.get("approval_record_sha256") != sha_obj(approval) or pr.get("ui_sha256") != rep["ui_sha256"]: raise Refused("the probe predates the current approval or UI script; re-probe")
    if not pr.get("pass"): raise Refused(f"the probe did not pass ({pr.get('status')}): " + "; ".join([*(pr.get("limitations") or []), str((pr.get("ui_traffic") or {}).get("reason") or "")]).strip("; "))
    RUNS.mkdir(parents=True, exist_ok=True)
    extra = {"approval.json": approval, "contract_source.json": {"path": rep["contract"], "sha256": rep["contract_sha256"], "images": rep["images"], "ui_sha256": rep["ui_sha256"]},
             "probe.json": {k: pr.get(k) for k in ("probe", "status", "pass", "contract_sha256", "approval_record_sha256", "ui_sha256", "capabilities", "limitations", "finished_utc")}
                           | {"ui_traffic": {k: (pr.get("ui_traffic") or {}).get(k) for k in ("status", "reason", "action_requests_intercepted", "verified_by_object")},
                              "selftest_checks": {k: v["pass"] for k, v in pr["selftest"]["checks"].items()}}}
    if external:
        extra.update({"agent_spec.json": external["spec"], "agent_approval.json": external["approval"],
                      "agent_spec_source.json": {"path": str(agent_spec), "sha256": external["sha256"]}})
    if rep["protocol"] in ("contract-v2", "contract-v3"):
        extra["probe.json"].update({k: pr.get(k) for k in ("observed", "declared", "declared_vs_observed", "applicability", "false_failure_attribution")})
        b, digest = GEN.run(path, list(candidates), RUNS, seed=seed, extra_files=extra, observed=pr.get("observed"), applicability=pr.get("applicability"),
                            false_failure_attribution=pr.get("false_failure_attribution"), scripted_version=scripted_version, external=external)
        return b, digest, verify_bundle(b)
    b, digest = GEN.run(path, list(candidates), RUNS, seed=seed, extra_files=extra, scripted_version=scripted_version, external=external)
    return b, digest, verify_bundle(b)

# ------------------------------------------------------------------------------------------------------------- verify
def scripted_version_check(b):
    """Which scripted-policy version produced the evidence; consistency across summary, environment and run results."""
    J = lambda n: json.loads((b / n).read_text())
    env, summ = J("environment.json"), J("summary.json")
    ver = env.get("scripted_policy_version")
    if ver is None: return {"scripted_policy_version": "unrecorded (bundle predates M4.1; v1 implied)", "problems": []}
    probs = []
    if summ.get("scripted_policy_version") != ver: probs.append("summary and environment disagree on the scripted-policy version")
    for f in sorted((b / "runs").glob("*/result.json")):
        doc = json.loads(f.read_text())
        if doc.get("external"): continue                                    # the external candidate is not a scripted policy
        rv = doc.get("scripted_policy_version", "v1")
        if rv != ver and "claim" in doc: probs.append(f"{f.parent.name}: result produced by scripted policy {rv}, bundle says {ver}")
    cur = GEN.SCRIPTED_SRC.get(ver)
    return {"scripted_policy_version": ver, "scripted_policy_sha256": env.get("scripted_agents_sha256"),
            "matches_current_file": bool(cur and cur.exists() and sha_file(cur) == env.get("scripted_agents_sha256")), "problems": probs}

def external_agent_check(b):
    """External-agent bundles: the approval covers the spec that ran, every external run names it, and each run's
    agent evidence is present (exit status, result validity, logs)."""
    if not (b / "agent_spec.json").exists(): return {"agent_kind": "scripted only", "problems": []}
    J = lambda n: json.loads((b / n).read_text()); probs = []
    try: src, ap = J("agent_spec_source.json"), J("agent_approval.json")
    except (OSError, ValueError) as e: return {"agent_kind": "external", "problems": [f"agent binding missing: {e}"]}
    if ap.get("agent_spec_sha256") != src.get("sha256"): probs.append("agent approval does not cover the agent spec that ran")
    n = 0
    for f in sorted((b / "runs").glob("*/result.json")):
        ext = json.loads(f.read_text()).get("external")
        if not ext: continue
        n += 1
        if ext.get("spec_sha256") != src.get("sha256"): probs.append(f"{f.parent.name}: run produced by a different agent spec")
        if "logs" not in ext or "exit_code" not in ext or "result_valid" not in ext: probs.append(f"{f.parent.name}: external agent evidence incomplete")
    return {"agent_kind": "external", "agent_spec_sha256": src.get("sha256"), "external_runs": n, "problems": probs}

def verify_bundle(b):
    b = Path(b)
    r = _verify_bundle(b)
    if r.get("files") is None: return r
    sv = scripted_version_check(b); ea = external_agent_check(b)
    return {**r, "ok": r["ok"] and not sv["problems"] and not ea["problems"], "problems": r["problems"] + sv["problems"] + ea["problems"],
            **{k: v for k, v in sv.items() if k != "problems"}, **{k: v for k, v in ea.items() if k != "problems"}}

def _verify_bundle(b):
    b = Path(b); probs = []
    m = B.verify(b)
    if not m["ok"]: return {"ok": False, "problems": [f"manifest: {m['problems']}"]}
    J = lambda n: json.loads((b / n).read_text())
    try: summ, prot, plan, st, td, v = J("summary.json"), J("protocol.json"), J("plan.json"), J("selftest.json"), J("teardown.json"), J("verdict.json")
    except (OSError, ValueError) as e: return {"ok": False, "problems": [f"missing bundle file: {e}"]}
    runs = [json.loads(l) for l in (b / "runs.jsonl").read_text().splitlines() if l.strip()]
    if prot.get("protocol") in ("contract-v2", "contract-v3"): return _verify_v2(b, m, summ, prot, plan, st, td, v, runs)
    if prot.get("protocol") != "contract-v1": probs.append("not a contract-v1 bundle")
    intent = int(re.search(r"exactly (\d+)", prot["workflow"]["authorised_intent"]).group(1))
    for r in runs:
        o = SO.outcome(r, intent, r["verified_by_object"].get(str(r["target"]), 0) == intent, r.get("claim") or {})
        for k in ("target_effects", "duplicate_target_effects", "other_object_effects", "accounting_agrees", "false_success_claim", "completed"):
            if o[k] != r.get(k): probs.append(f"{r['run_id']}: stored {k}={r.get(k)!r} but recomputed {o[k]!r}")
    if SO.verdict(prot, plan, runs, st, td) != v: probs.append("verdict.json does not match the verdict recomputed from runs.jsonl")
    ev = GEN.evidence_completeness(plan, runs, b)
    if ev != summ.get("evidence_completeness"): probs.append("evidence_completeness does not match the bundle's runs")
    valid_and_complete = v["harness_validity"]["valid"] and ev["complete"]
    if summ.get("qualification_status", "").startswith("QUALIFICATION RESULT") != valid_and_complete: probs.append("qualification_status inconsistent with harness validity / evidence")
    try:
        ap, src = J("approval.json"), J("contract_source.json"); con = J("contract.json")
        if ap["contract_sha256"] != src["sha256"]: probs.append("approval does not cover the contract that ran")
        if sorted(ap["images"]) != sorted(images_of(con)) or sorted(src["images"]) != sorted(images_of(con)): probs.append("approved images differ from the images in the bundle's contract")
    except (OSError, ValueError, KeyError) as e: probs.append(f"approval binding missing: {e}")
    return {"ok": not probs, "digest": m["digest"], "files": m["files"], "problems": probs, "harness_valid": v["harness_validity"]["valid"],
            "evidence_complete": ev["complete"], "qualification_status": summ.get("qualification_status")}

V2_FIELDS = ("target_effects", "duplicate_target_effects", "other_object_effects", "accounting_agrees", "attribution", "fidelity", "protection",
             "application_protected", "recovery", "claim_class", "completed", "unclassified_writes")

V3_FIELDS = ("pending_at_horizon", "commit_windows", "lifecycle", "early_read", "run_effect_safety", "horizon")

def _verify_v2(b, m, summ, prot, plan, st, td, v, runs):
    """contract-v2: recompute every per-run classification and the verdict from the stored inputs."""
    probs = []
    J = lambda n: json.loads((b / n).read_text())
    try: env, pr = J("environment.json"), J("probe.json")
    except (OSError, ValueError) as e: return {"ok": False, "problems": [f"missing bundle file: {e}"]}
    ctx = env.get("v2_ctx") or {}
    v3 = prot["protocol"] == "contract-v3"
    con = J("contract.json")
    want_ctx = {**GEN.v2_ctx(con, pr.get("observed")), "applicability": pr.get("applicability") or [], "false_failure_attribution": pr.get("false_failure_attribution")}
    if v3:
        hook = bool((con.get("fault_hooks") or {}).get("late_backend_commit"))
        want_ctx.update({"late_hook": hook, "late_commit_delay_s": prot["late_commit_delay_s"],
                         "not_applicable_conditions": [] if hook else [f"{c}: no late_backend_commit fault hook in the contract" for c in SV3.LATE]})
    if ctx != want_ctx: probs.append("v2/v3 context does not match the bundle's contract and probe record")
    S = SV3 if v3 else SV2
    fields = V2_FIELDS + (V3_FIELDS if v3 else ())
    for r in runs:
        c = S.classify_run(r, ctx)
        for k in fields:
            if c[k] != r.get(k): probs.append(f"{r['run_id']}: stored {k} differs from the recomputed value")
    if S.verdict(prot, plan, runs, st, td, ctx) != v: probs.append("verdict.json does not match the verdict recomputed from runs.jsonl")
    ev = GEN.evidence_completeness(plan, runs, b)
    if ev != summ.get("evidence_completeness"): probs.append("evidence_completeness does not match the bundle's runs")
    if summ.get("qualification_status", "").startswith("QUALIFICATION RESULT") != (v["harness_validity"]["valid"] and ev["complete"]): probs.append("qualification_status inconsistent with harness validity / evidence")
    try:
        ap, src, con = J("approval.json"), J("contract_source.json"), J("contract.json")
        if ap["contract_sha256"] != src["sha256"]: probs.append("approval does not cover the contract that ran")
        if sorted(ap["images"]) != sorted(images_of(con)): probs.append("approved images differ from the images in the bundle's contract")
        if not ap.get("destructive_controls_accepted"): probs.append("approval does not record acceptance of the destructive controls")
    except (OSError, ValueError, KeyError) as e: probs.append(f"approval binding missing: {e}")
    return {"ok": not probs, "digest": m["digest"], "files": m["files"], "problems": probs, "protocol": prot["protocol"], "harness_valid": v["harness_validity"]["valid"],
            "evidence_complete": ev["complete"], "qualification_status": summ.get("qualification_status")}
