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
from . import bundle as B, config as C, contract as K, dockerops as D, generic as GEN, scoring_objects as SO

ROOT = Path(__file__).resolve().parent.parent
STATE = C.VAR / "workflow"
APPROVALS, PROBES, RUNS = STATE / "approvals", STATE / "probes", STATE / "runs"
PINNED = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
SERVICE_KEYS = {"name", "image", "entrypoint", "args", "port", "uid", "env", "writable", "health", "public", "mounts", "sysctls", "note"}
FORBIDDEN_WRITABLE = {"/", "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib", "/boot", "/run"}
URL = re.compile(r"\b([a-z][a-z0-9+.-]*)://(?:[^/@\s]*@)?([^/:?#\s\"'{}]+)", re.I)
WRITE_SQL = re.compile(r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|replace|merge|copy)\b", re.I)
INDEPENDENCE = re.compile(r"\bL([1-3])\b")

class Refused(Exception): pass

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
    if c.get("contract") != "afd-workflow-contract/1.1":
        probs.append("contract must be afd-workflow-contract/1.1 (per-object accounting: objects.target, decoys, family oracle)")
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
    return {"pass": not probs, "contract": str(path), "contract_name": c.get("name"), "contract_sha256": sha_file(path), "protocol": K.protocol_for(c)["protocol"],
            "problems": probs, "warnings": warns, "images": images_of(c), "ui": (ui and ("steps" if ui.get("steps") else "none")), "ui_sha256": ui_sha}

# ------------------------------------------------------------------------------------------------------------- approval
def _slug(name): return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "contract"

def approve(path, approver, ui_path=None):
    if not approver or not re.fullmatch(r"[\w .@-]{2,80}", approver): raise Refused("--approver NAME is required (2-80 characters)")
    rep = check(path, ui_path)
    if not rep["pass"]: raise Refused("check failed; nothing approved:\n  - " + "\n  - ".join(rep["problems"]))
    rec = {"approval": "afd-contract-approval/1", "contract": rep["contract"], "contract_name": rep["contract_name"], "contract_sha256": rep["contract_sha256"],
           "images": rep["images"], "ui_sha256": rep["ui_sha256"], "protocol": rep["protocol"], "check_report_sha256": sha_obj(rep),
           "approver": approver, "approved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
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

def probe(path, ui_path=None, delay=6):
    from . import cdp_driver, generic_llm as GL
    approval, rep = require_approval(path, ui_path); require_local_images(rep["images"])
    ui, ui_sha = load_ui(ui_path) if ui_path else (None, None)
    c = K.load(path); res = {"probe": "afd-contract-probe/1", "contract_sha256": rep["contract_sha256"], "approval_record_sha256": sha_obj(approval), "ui_sha256": ui_sha,
                             "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with GL.LLMEpisodeJob(c, "probe", with_gateway=False) as job:
        st = GEN.selftest_objects(job, delay); res["selftest"] = st
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
    chk = st["checks"]
    res["capabilities"] = {"duplicate_detectable": chk["G2_lost_ack_duplicate_detectable"]["pass"] and chk["G3b_delayed_commit_duplicate_detectable"]["pass"],
                           "late_commit_detectable": chk["G3a_delayed_commit_lands_late"]["pass"], "other_object_detectable": chk["G6_other_object_effect_detected"]["pass"]}
    lim = []
    if chk["G1_clean"]["pass"] and not chk["G2_lost_ack_duplicate_detectable"]["pass"] and chk["G2_lost_ack_duplicate_detectable"].get("after_second") == 1:
        lim.append("an identical resubmission did not create a second effect (native duplicate prevention suspected): the duplicate positive control cannot validate this harness; a qualification would be NOT ASSESSED")
    if res["ui_traffic"]["status"] == "NOT_APPLICABLE": lim.append(f"no UI path for the action ({res['ui_traffic']['reason']}): UI traffic not verified; candidates exercise the API only")
    res["limitations"] = lim
    res["pass"] = bool(st["pass"]) and res["ui_traffic"]["status"] in ("PASS", "NOT_APPLICABLE")
    res["status"] = "PASS" + (" (with limitations)" if lim else "") if res["pass"] else "FAIL"
    res["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    PROBES.mkdir(parents=True, exist_ok=True); (PROBES / f"{rep['contract_sha256']}.json").write_text(json.dumps(res, indent=1, default=str))
    return res

# ------------------------------------------------------------------------------------------------------------- qualify
def qualify(path, ui_path=None, candidates=("scripted_trust_response", "scripted_verify_no_resubmit"), agent="scripted", seed=None):
    if agent != "scripted": raise Refused("only --agent scripted is enabled ($0); LLM candidates are not part of M3")
    for p in candidates: GEN.validate_scripted(p)
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
    b, digest = GEN.run(path, list(candidates), RUNS, seed=seed, extra_files=extra)
    return b, digest, verify_bundle(b)

# ------------------------------------------------------------------------------------------------------------- verify
def verify_bundle(b):
    b = Path(b); probs = []
    m = B.verify(b)
    if not m["ok"]: return {"ok": False, "problems": [f"manifest: {m['problems']}"]}
    J = lambda n: json.loads((b / n).read_text())
    try: summ, prot, plan, st, td, v = J("summary.json"), J("protocol.json"), J("plan.json"), J("selftest.json"), J("teardown.json"), J("verdict.json")
    except (OSError, ValueError) as e: return {"ok": False, "problems": [f"missing bundle file: {e}"]}
    if prot.get("protocol") != "contract-v1": probs.append("not a contract-v1 bundle")
    runs = [json.loads(l) for l in (b / "runs.jsonl").read_text().splitlines() if l.strip()]
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
