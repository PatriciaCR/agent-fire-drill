"""Run a refill qualification and write a sealed evidence bundle.
Usage: python3 scripts/qualify.py --profile full|mini|smoke --policy generic_guard [--policy cautious_no_resubmit]
                                  [--condition lost_ack ...] [--budget 1.50] [--seed N]
  full   the frozen protocol (protocols/refill-v1.json): every candidate x 4 conditions x n=5, positive control 2 x 3
  mini   n=1 per cell, positive control 1 per condition: exercises the engine end to end; NOT a qualification
  smoke  one policy, one condition, one episode, quick self-test: what the web interface runs"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from afd import config as C, firewall as F, qualify as Q  # noqa: E402

ap = argparse.ArgumentParser(); ap.add_argument("--profile", default="full", choices=Q.PROFILES)
ap.add_argument("--policy", action="append", required=True); ap.add_argument("--condition", action="append")
ap.add_argument("--budget", type=float); ap.add_argument("--seed", type=int)
a = ap.parse_args()
if not C.ROOTLESS: F.install()          # dev (rootful) only; in rootless mode the host firewall is a root-installed service
out = Q.run(a.policy, a.profile, a.condition, a.budget, a.seed, progress=lambda s: print("…", s["state"], f"{s['done']}/{s['total']}", flush=True))
s = json.loads((out / "summary.json").read_text())
print(json.dumps({"bundle": str(out), "digest_manifest_sha256": json.loads(json.dumps(__import__("afd.bundle", fromlist=["verify"]).verify(out)))["digest"],
                  "spend": s["spend"], "verdict": s["verdict"]}, indent=1))
