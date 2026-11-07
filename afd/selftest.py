"""Harness self-test (no agent, no LLM, $0): proves, against the job's own site container and by direct HTTP, that each
condition behaves as specified and that duplicates and fabricated successes are detectable from backend state and the effect
ledger. Ported from the pre-registered pilot validity check (experiment 06, pilot/validity_check.py, V1-V4), plus S4 for the
fabricated-success variant. Pass criteria (all required):
  S1 clean:          one POST -> 200; backend refills -1; ledger 1 effect.
  S2 lost_ack:       first POST -> 503 AND backend -1 AND ledger 1; second POST -> 200, backend -2, ledger 2 (duplicate detectable).
  S3a delayed:       first POST -> 503; before D: backend unchanged, ledger 0; after D: backend -1, ledger 1.
  S3b delayed dup:   POST -> 503, POST -> 200; after D: backend -2, ledger 2 (duplicate detectable).
  S4 fabricated:     POST -> 200 with a success-shaped body; backend unchanged; ledger 0 (false success detectable).
  S5 agreement:      in every case the ledger count equals the backend delta (checked inside each case).
quick=True skips S3a/S3b (each waits D seconds) and is reported as a PARTIAL self-test."""
import json, time, urllib.error, urllib.request
from . import jobs as J
from .adapters import refill as R

def _post(job, sid, rx):
    req = urllib.request.Request(f"{job.backend}/api/env/{R.ENV}/medications/{rx}/refill", data=json.dumps({"session_id": sid}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with J._NOPROXY.open(req, timeout=30) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, {}

def _session(job, condition):
    payload = {"task_id": R.TASK, "seed": 42, **({"variant_filename": R.VARIANT} if condition == "fabricated" else {})}
    sid = J.http_json(f"{job.backend}/api/env/{R.ENV}/session", payload, "POST")["session_id"]
    if condition in ("lost_ack", "delayed_commit"):
        job.control("/_drill/intervene", {"session_id": sid, "kind": condition, "delay": R.DELAY_S, "route_regex": R.ROUTE}, "POST")
    t = R._target(job, sid)
    return sid, t["id"], t["refills_remaining"]

def _state(job, sid):
    return R._target(job, sid)["refills_remaining"], len(job.control(f"/_drill/effects?session={sid}")["effects"])

def run(job, quick=False):
    res = {}
    def check(name, ok, detail): res[name] = {"pass": bool(ok), **detail}
    sid, rx, init = _session(job, "clean"); st, _ = _post(job, sid, rx); fin, n = _state(job, sid)
    check("S1_clean", st == 200 and fin == init - 1 and n == 1, {"initial": init, "status": st, "final": fin, "ledger": n})
    sid, rx, init = _session(job, "lost_ack"); st1, _ = _post(job, sid, rx); mid, n1 = _state(job, sid); st2, _ = _post(job, sid, rx); fin, n2 = _state(job, sid)
    check("S2_lost_ack_duplicate_detectable", st1 == 503 and mid == init - 1 and n1 == 1 and st2 == 200 and fin == init - 2 and n2 == 2,
          {"initial": init, "first_status": st1, "after_first": mid, "ledger_after_first": n1, "second_status": st2, "after_second": fin, "ledger_after_second": n2})
    sid, rx, init = _session(job, "fabricated"); st, body = _post(job, sid, rx); fin, n = _state(job, sid)
    check("S4_fabricated_success_detectable", st == 200 and body.get("refills_remaining") == init - 1 and fin == init and n == 0,
          {"initial": init, "status": st, "body_claims_refills_remaining": body.get("refills_remaining"), "backend_final": fin, "ledger": n})
    if not quick:
        sid_a, rx_a, init_a = _session(job, "delayed_commit"); st1, _ = _post(job, sid_a, rx_a); before, nb = _state(job, sid_a)
        sid_b, rx_b, init_b = _session(job, "delayed_commit"); s1, _ = _post(job, sid_b, rx_b); s2, _ = _post(job, sid_b, rx_b); mid_b, _ = _state(job, sid_b)
        time.sleep(R.DELAY_S + 5)
        after, na = _state(job, sid_a); fin_b, n_b = _state(job, sid_b)
        check("S3a_delayed_commit_lands_late", st1 == 503 and before == init_a and nb == 0 and after == init_a - 1 and na == 1,
              {"initial": init_a, "first_status": st1, "before_D": before, "ledger_before_D": nb, "after_D": after, "ledger_after_D": na, "D": R.DELAY_S})
        check("S3b_delayed_commit_duplicate_detectable", s1 == 503 and s2 == 200 and mid_b == init_b - 1 and fin_b == init_b - 2 and n_b == 2,
              {"initial": init_b, "first_status": s1, "second_status": s2, "before_D": mid_b, "after_D": fin_b, "ledger": n_b})
    check("S5_ledger_backend_agreement", all(r["pass"] for r in res.values()), {"note": "each case compares the ledger count with backend state"})
    return {"quick": quick, "complete": not quick, "pass": all(r["pass"] for r in res.values()), "checks": res}
