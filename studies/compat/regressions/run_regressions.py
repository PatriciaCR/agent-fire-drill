"""Regenerate the failing real-browser evidence for the three issues the study found ($0). Each case runs the normal
browser check with ONE thing reverted, and records what went wrong. Output: studies/compat/results/regressions/*.json"""
import json, shutil, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "studies" / "compat"))
from afd import generic as GEN  # noqa: E402
import browser as BR  # noqa: E402
OUT = ROOT / "studies" / "compat" / "results" / "regressions"; OUT.mkdir(parents=True, exist_ok=True)
CASES = [("keepalive_replay_bug", "vikunja", "delayed_commit", ROOT / "studies/compat/regressions/proxy_prefix_replay.py"),
         ("host_rewrite_bug", "gitea", "lost_ack", ROOT / "studies/compat/regressions/proxy_prefix_host.py"),
         ("doc_derived_contract", "vikunja-docroute", "lost_ack", None)]
for name, app, cond, proxy in CASES:
    GEN.PROXY_SRC = proxy or (ROOT / "afd" / "faults" / "proxy.py")
    BR.main(app, cond)
    res = ROOT / "studies" / "compat" / "results" / f"{app}-browser" / f"{cond}.json"
    d = json.loads(res.read_text()); d["regression_case"] = name; d["proxy_used"] = str(GEN.PROXY_SRC.relative_to(ROOT))
    (OUT / f"{name}.json").write_text(json.dumps(d, indent=1))
    png = d["steps"][-1].get("png")
    if png: shutil.copy(res.parent / png, OUT / f"{name}.png")
    print(name, "->", {"verified": d["verified"], "proxy_inferred": d["proxy_inferred"], "proxy_requests": [(r["status_to_client"], r["intervention"], r["outcome"]) for r in d["proxy_requests"]],
                       "unclassified_writes": [(w["method"], w["path"]) for w in d.get("other_writes", []) if w["class"] == "unclassified"],
                       "app_log": [l[-150:] for l in d.get("app_log_writes", [])][-3:]}, flush=True)
