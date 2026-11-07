"""Orchestrator daemon: executes requests from the spool one at a time.

The web interface (afd-web) can only drop small JSON requests into SPOOL_DIR; it has no Docker access and no key. This daemon
validates every request against a strict schema (preset names and protocol conditions only, never free text), enforces the
daily spend cap and the smoke quota, runs the request through afd.qualify, and publishes status files to
RESULTS_DIR/_requests/<id>.json (readable by the web). Admin requests (kind "qualify") are written by root via afdctl."""
import json, os, re, sys, time, traceback
from pathlib import Path
from . import config as C, jobs as J, qualify as Q

REQ_DIR = C.RESULTS_DIR / "_requests"
ID_RE = re.compile(r"^[a-z0-9-]{8,64}$")
POLL_S = 2

def _write_status(rid, **k):
    REQ_DIR.mkdir(parents=True, exist_ok=True)
    p = REQ_DIR / f"{rid}.json"
    try: cur = json.loads(p.read_text())
    except Exception: cur = {"id": rid}
    cur.update(k, updated=time.time())
    tmp = REQ_DIR / f".{rid}.tmp"; tmp.write_text(json.dumps(cur, default=str)); os.chmod(tmp, 0o640); tmp.replace(p)

def spend_last_24h():
    cutoff, total = time.time() - 86400, 0.0
    for p in C.LEDGER_DIR.glob("*.jsonl"):
        try:
            for l in p.read_text().splitlines():
                if l.strip():
                    r = json.loads(l)
                    if r.get("t", 0) >= cutoff: total += float(r.get("cost_usd", 0))
        except (OSError, ValueError): continue
    return round(total, 6)

def smokes_last_24h():
    cutoff = time.time() - 86400; n = 0
    for p in REQ_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
            if d.get("kind") == "smoke" and d.get("accepted_at", 0) >= cutoff: n += 1
        except (OSError, ValueError): continue
    return n

def validate(req):
    """Return (normalised request, None) or (None, reason)."""
    protocol, _ = Q.load_protocol()
    if not isinstance(req, dict) or not ID_RE.match(str(req.get("id", ""))): return None, "bad id"
    kind = req.get("kind")
    if kind == "smoke":
        pol, cond = req.get("policy"), req.get("condition")
        if pol not in (*Q.CANDIDATE_PRESETS, Q.POSITIVE_CONTROL): return None, "unknown preset"
        if cond not in protocol["conditions"]: return None, "unknown condition"
        return {"id": req["id"], "kind": "smoke", "policies": [pol], "conditions": [cond], "profile": "smoke", "budget": C.SMOKE_BUDGET_USD}, None
    if kind == "qualify":
        pols, prof = req.get("policies"), req.get("profile", "full")
        if not isinstance(pols, list) or not pols or any(p not in Q.CANDIDATE_PRESETS for p in pols) or len(set(pols)) != len(pols): return None, "policies must be candidate presets"
        if prof not in ("full", "mini"): return None, "profile must be full or mini"
        budget = float(req.get("budget", 1.50 if prof == "full" else 0.25))
        if not 0 < budget <= C.DAILY_BUDGET_USD: return None, "budget out of range"
        return {"id": req["id"], "kind": "qualify", "policies": pols, "conditions": None, "profile": prof, "budget": budget}, None
    return None, "unknown kind"

def handle(path):
    try: raw = json.loads(path.read_text())
    except (OSError, ValueError): path.unlink(missing_ok=True); return
    path.unlink(missing_ok=True)
    req, why = validate(raw)
    rid = raw.get("id") if isinstance(raw, dict) and ID_RE.match(str(raw.get("id", ""))) else None
    if not rid: return
    if why: return _write_status(rid, state="rejected", reason=why)
    if req["kind"] == "smoke" and smokes_last_24h() >= C.SMOKES_PER_DAY:
        return _write_status(rid, state="rejected", kind="smoke", reason="daily smoke quota reached")
    spent = spend_last_24h()
    if spent + req["budget"] > C.DAILY_BUDGET_USD:
        return _write_status(rid, state="rejected", kind=req["kind"], reason=f"daily budget: ${spent:.2f} spent in 24 h, cap ${C.DAILY_BUDGET_USD:.2f}")
    _write_status(rid, state="accepted", kind=req["kind"], accepted_at=time.time(), policies=req["policies"], conditions=req["conditions"], profile=req["profile"])
    try:
        out = Q.run(req["policies"], req["profile"], req["conditions"], req["budget"], requested_by=f"{req['kind']}:{rid}",
                    progress=lambda s: _write_status(rid, state="running", progress=s))
        _write_status(rid, state="done", bundle=out.name)
    except Exception as e:
        _write_status(rid, state="failed", reason=f"{type(e).__name__}: {str(e)[:300]}")
        traceback.print_exc()

def main():
    C.SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"afd-orchd: mode={C.MODE} spool={C.SPOOL_DIR} results={C.RESULTS_DIR}; janitor: {J.janitor()}", flush=True)
    while True:
        reqs = sorted((p for p in C.SPOOL_DIR.glob("*.json") if not p.name.startswith(".")), key=lambda p: p.stat().st_mtime)
        if reqs: handle(reqs[0])
        else: time.sleep(POLL_S)

if __name__ == "__main__":
    os.umask(0o027); main()
