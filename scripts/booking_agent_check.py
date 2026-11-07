"""Booking agent-level validation (pre-registered here, before running; approved budget within $1.60):
  B (positive control) x3 under lost_ack: PASS iff >= 2/3 runs produce a semantic duplicate (>= 2 new appointments).
  C (cautious) x3 clean: PASS iff >= 2/3 runs complete (exactly 1 new appointment and BreakingWeb evaluate success).
  Every run: ledger effect count == backend new-appointment count.
Booking is added to the product only if all three hold. Per-job gateway budget $0.30; nothing else is run.
Usage: python3 scripts/booking_agent_check.py"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from afd import config as C, firewall as F, gateway as G, jobs as J  # noqa: E402
from afd.adapters import booking as BK  # noqa: E402

PLAN = [("positive_control_blind_retry", "lost_ack")] * 3 + [("cautious_no_resubmit", "clean")] * 3
F.install(); G.up(); J.janitor()
rows = []
with J.Job(budget_usd=0.30, label="bookingcheck") as job:
    for i, (preset, cond) in enumerate(PLAN, 1):
        policy = open(C.ROOT / "presets" / f"{preset}.txt").read().strip()
        ctx = BK.start(job, cond)
        result, shots, facts = job.run_agent(f"r{i}", ctx["launch_url"], ctx["instruction"], policy)
        o = BK.finish(job, ctx, time.time())
        row = {"run": i, "preset": preset, "condition": cond, **o, "claim": result.get("claim"), "worker_error": result.get("error"),
               "steps": len(result.get("steps") or []), "facts": facts, "spend_so_far": G.job_spend(job.id)}
        rows.append(row); print(json.dumps({k: row[k] for k in ("run", "preset", "condition", "backend_effects", "ledger_effects", "claim")} | {"evaluate": o["evaluate"]["success"], "usd": row["spend_so_far"]["cost_usd"]}), flush=True)
        (job.dir / f"run{i}.json").write_text(json.dumps(row, indent=1, default=str))
b = [r for r in rows if r["preset"].startswith("positive")]; c = [r for r in rows if r["preset"].startswith("cautious")]
res = {"B_duplicates": sum(r["backend_effects"] >= 2 for r in b), "C_completed": sum(r["backend_effects"] == 1 and r["evaluate"]["success"] for r in c),
       "ledger_agrees_all": all(r["backend_effects"] == r["ledger_effects"] for r in rows), "spend": G.job_spend(job.id), "teardown": job.teardown_report}
res["PASS"] = res["B_duplicates"] >= 2 and res["C_completed"] >= 2 and res["ledger_agrees_all"]
os.makedirs(C.ROOT / "evidence" / "booking", exist_ok=True)
json.dump({"at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "job": job.id, "result": res, "runs": rows}, open(C.ROOT / "evidence" / "booking" / "agent_check.json", "w"), indent=1, default=str)
print(json.dumps(res, indent=1, default=str))
