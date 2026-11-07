"""M1 regression pass ($0: scripted policies only, no inference). Writes ONLY studies/compat/results/m1/ and var/compat-m1/;
the study's own results (results/final, results/<app>-*, results/regressions) are never written.

Per app (vikunja, gitea, listmonk, woocommerce):
  A. contract-v0 regression: the UNCHANGED v1 contract through the core pipeline, compared field by field with the
     frozen final pass (results/final/summary.json): self-test checks, harness validity and its checks, positive control,
     candidate statuses, effect-safety status and completion counts.
  B. contract-v1: the derived v1.1 contract (per-object accounting) with the two study candidates plus
     scripted_wrong_target (an unsafe-by-construction fixture), reporting per-object outcomes.
Not re-run here (they write into the study's result files): measure.py and browser.py. With no objects configured, the
proxy's request path is unchanged (tests/test_contract.py: primitives, keep-alive replay and Host regressions)."""
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from afd import bundle as B, firewall as F, gateway as G, generic as GEN  # noqa: E402

APPS = sys.argv[1:] or ["vikunja", "gitea", "listmonk", "woocommerce"]
OUT = ROOT / "studies" / "compat" / "results" / "m1"; OUT.mkdir(parents=True, exist_ok=True)
WORK = ROOT / "var" / "compat-m1"; WORK.mkdir(parents=True, exist_ok=True)
FINAL = json.loads((ROOT / "studies" / "compat" / "results" / "final" / "summary.json").read_text())
CANDS = ["scripted_trust_response", "scripted_verify_no_resubmit"]

def core_view(v, st):
    return {"selftest": {k: x["pass"] for k, x in st["checks"].items()}, "harness_valid": v["harness_validity"]["valid"],
            "harness_checks": v["harness_validity"]["checks"],
            "positive_control_duplicate_runs": v["harness_validity"]["positive_control"].get("duplicate_runs"),
            "candidates": {p: {"status": c["status"], "effect_safety": (c.get("effect_safety") or {}).get("status"),
                               "completed": (c.get("completion") or {}).get("completed")} for p, c in v["candidates"].items()}}

def final_view(app):
    c = FINAL["apps"][app]["core"]
    return {"selftest": {k: x["pass"] for k, x in c["selftest"].items()}, "harness_valid": c["harness_valid"], "harness_checks": c["harness_checks"],
            "positive_control_duplicate_runs": c["positive_control"].get("duplicate_runs"),
            "candidates": {p: {"status": x["status"], "effect_safety": (x.get("effect_safety") or {}).get("status"),
                               "completed": (x.get("completion") or {}).get("completed")} for p, x in c["candidates"].items()}}

def load(b): return json.loads((b / "verdict.json").read_text()), json.loads((b / "selftest.json").read_text()), [json.loads(l) for l in (b / "runs.jsonl").read_text().splitlines() if l.strip()]

def main():
    F.install(); G.up()
    res_file = OUT / "summary.json"
    summary = json.loads(res_file.read_text()) if res_file.exists() else {"apps": {}}
    for app in APPS:
        rec = {"at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        print(f"== {app}: A contract-v0 regression", flush=True)
        try:
            b, dig = GEN.run(ROOT / "studies" / "compat" / "contracts" / f"{app}-v1.json", CANDS, WORK, seed=20261107)
            v, st, _ = load(b); now, then = core_view(v, st), final_view(app)
            rec["A_contract_v0"] = {"bundle": b.name, "bundle_verifies": B.verify(b)["ok"], "now": now, "final_pass": then,
                                    "regression": {k: {"now": now[k], "final_pass": then[k]} for k in now if now[k] != then[k]}}
        except Exception as e:
            rec["A_contract_v0"] = {"error": f"{type(e).__name__}: {str(e)[:400]}"}
        print(f"== {app}: B contract-v1 per-object", flush=True)
        try:
            b, dig = GEN.run(ROOT / "studies" / "compat" / "contracts" / f"{app}-v1.1.json", CANDS + ["scripted_wrong_target"], WORK, seed=20261107)
            v, st, runs = load(b)
            rec["B_contract_v1"] = {"bundle": b.name, "bundle_verifies": B.verify(b)["ok"], **core_view(v, st),
                                    "selftest_G6_G7": {k: st["checks"][k] for k in ("G6_other_object_effect_detected", "G7_fabricated_target_then_decoy_write") if k in st["checks"]},
                                    "accounting_mismatches": v["harness_validity"]["accounting_mismatches"],
                                    "effect_safety": {p: (c.get("effect_safety") or {}).get("statement") or c.get("reason") for p, c in v["candidates"].items()},
                                    "unclassified_write_attempts_total": v["unclassified_write_attempts_total"],
                                    "wrong_target_fabricated_runs": [{k: r[k] for k in ("run_id", "target", "verified_by_object", "inferred_by_object", "attempts_by_object",
                                                                                         "target_effects", "other_object_effects", "accounting_agrees", "false_success_claim")}
                                                                     for r in runs if r["policy"] == "scripted_wrong_target" and r["condition"] == "fabricated"]}
        except Exception as e:
            rec["B_contract_v1"] = {"error": f"{type(e).__name__}: {str(e)[:400]}"}
        summary["apps"][app] = rec
        res_file.write_text(json.dumps(summary, indent=1, default=str))
        print(json.dumps({app: {"A_regression": rec["A_contract_v0"].get("regression", rec["A_contract_v0"].get("error")),
                                "B_harness_valid": rec["B_contract_v1"].get("harness_valid", rec["B_contract_v1"].get("error")),
                                "B_effect_safety": rec["B_contract_v1"].get("effect_safety")}}, default=str), flush=True)
    print("M1 REGRESSION DONE", flush=True)

if __name__ == "__main__":
    main()
