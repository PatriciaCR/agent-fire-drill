"""Stage 0 of the compatibility study: run an app's best-effort contract-v0 through the UNCHANGED generic pipeline and
record exactly where it breaks (no retries, no workarounds). Usage: python3 studies/compat/stage0.py <contract.json>"""
import json, sys, time, traceback
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from afd import contract as K, dockerops as D, generic as GEN, firewall as F, gateway as G  # noqa: E402
c_path = Path(sys.argv[1]).resolve(); out = {"contract": str(c_path.relative_to(ROOT)), "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stage": "v0-unchanged"}
try:
    c = K.load(c_path); out["contract_valid"] = True
except Exception as e:
    out.update(contract_valid=False, failed_at="contract validation", error=f"{type(e).__name__}: {e}"); c = None
if c:
    F.install(); G.up()
    try:
        with GEN.GenericJob(c, "compat0") as job:
            out["app_started"] = True
            logs = D.sh("docker", "logs", "--tail", "15", job.app, check=False)
            out["app_log_tail"] = (logs.stdout + logs.stderr)[-1500:]
            try:
                s = K.Scenario(c, job.app_base); out["scenario"] = {"scope": s.scope, "item": s.item, "baseline": s.baseline}
                st = GEN.selftest(job, 3); out["selftest"] = st; out["failed_at"] = None if st["pass"] else "self-test"
            except Exception as e:
                out.update(failed_at="scenario setup / self-test", error=f"{type(e).__name__}: {e}")
    except Exception as e:
        out.update(app_started=False, failed_at="app start / health", error=f"{type(e).__name__}: {str(e)[-600:]}")
dst = ROOT / "studies" / "compat" / "results" / f"{c_path.stem}-stage0.json"; dst.write_text(json.dumps(out, indent=1, default=str))
print(json.dumps({k: v for k, v in out.items() if k not in ("selftest",)}, indent=1, default=str)[:2500])
