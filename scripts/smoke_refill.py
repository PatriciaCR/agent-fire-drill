"""End-to-end smoke: one refill episode through the product path (untrusted worker -> gateway -> Vultr), scored from backend state.
Usage: python3 scripts/smoke_refill.py [condition] [policy_file]"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from afd import config as C, firewall as F, gateway as G, jobs as J  # noqa: E402
from afd.adapters import refill as R  # noqa: E402

cond = sys.argv[1] if len(sys.argv) > 1 else "clean"; policy = open(sys.argv[2]).read().strip() if len(sys.argv) > 2 else ""
F.install(); G.up(); print("janitor:", J.janitor())
with J.Job(budget_usd=0.10, label="smoke") as job:
    ctx = R.start(job, cond)
    result, shots, facts = job.run_agent("r1", ctx["launch_url"], ctx["instruction"], policy)
    outcome = R.finish(job, ctx, time.time())
    spend = G.job_spend(job.id)
    out = {"job": job.id, "condition": cond, "outcome": outcome, "claim": result.get("claim"), "worker_error": result.get("error"),
           "steps": len(result.get("steps") or []), "screenshots": len(shots), "facts": facts, "gateway_spend": spend}
    (job.dir / "smoke.json").write_text(json.dumps(out, indent=1, default=str))
    for name, data in shots.items(): (job.dir / name).parent.mkdir(parents=True, exist_ok=True); (job.dir / name).write_bytes(data)
print(json.dumps({**out, "teardown": job.teardown_report}, indent=1, default=str)[:3000])
