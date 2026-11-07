"""M4.1 before/after comparison ($0, scripted only), through the real CLI: identical contracts, UI scripts and seed,
qualified with the historical scripted policy (v1) and the corrected one (v2). Uses the existing operator approvals
and probes. Writes studies/m41/results/comparison.json. Original bundles and results are not touched."""
import json, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
AFD = [sys.executable, str(ROOT / "bin" / "afd")]
OUT = ROOT / "studies" / "m41" / "results"; OUT.mkdir(parents=True, exist_ok=True)
C, A = "studies/compat/contracts", "studies/compat/apps"
CASES = [("contract-v1", a, f"{C}/{a}-v1.1.json", f"{A}/{a}.json") for a in ("vikunja", "gitea", "listmonk")] + \
        [("contract-v2", a, f"{C}/{a}-v1.2.json", f"{A}/{a}.json") for a in ("woocommerce", "vikunja", "listmonk")]

def cli(*args):
    r = subprocess.run([*AFD, *args], cwd=ROOT, capture_output=True, text=True, timeout=5400)
    try: j = json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError: j = None
    return r.returncode, j, r.stderr.strip()[-600:]

def summarise(bundle):
    b = Path(bundle); runs = [json.loads(l) for l in (b / "runs.jsonl").read_text().splitlines() if l.strip()]
    v = json.loads((b / "verdict.json").read_text()); out = {}
    for p, c in v["candidates"].items():
        mine = [r for r in runs if r["policy"] == p and r["role"] == "candidate" and r["category"] == "agent"]
        res = {r["run_id"]: json.loads((b / "runs" / r["run_id"] / "result.json").read_text()) for r in mine}
        checks = {rid: [s for s in x.get("steps", []) if "check" in (s["actions"][0] if s.get("actions") else {})] for rid, x in res.items()}
        rb = {rid: x.get("readback") for rid, x in res.items()}
        claimed = {rid: (x.get("claim") or {}).get("claimed_success") for rid, x in res.items()}
        eff = {r["run_id"]: r["target_effects"] for r in mine}
        out[p] = {"runs": len(mine),
                  "readback_attempted_runs": sum(bool(checks[r]) for r in checks),
                  "readback_confirmed_runs": (sum(any(x["confirmed"] for x in (rb[r] or [])) for r in rb) if any(rb.values()) or all(isinstance(rb[r], list) for r in rb)
                                              else f"not recorded by v1 (inferred from claims: {sum(bool(claimed[r]) for r in claimed if checks[r])})"),
                  "lookup_outcomes": sorted({x["lookup"] for r in rb for x in (rb[r] or [])}),
                  "claimed_success_runs": sum(bool(c) for c in claimed.values()),
                  "claimed_success_with_effect": sum(bool(claimed[r]) and eff[r] >= 1 for r in claimed),
                  "claimed_failure_with_effect": sum((not claimed[r]) and eff[r] >= 1 for r in claimed),
                  "effect_safety": c.get("effect_safety", {}).get("status"), "outcome_integrity": c.get("outcome_integrity", {}).get("status"),
                  "false_success_claims": c.get("outcome_integrity", {}).get("false_success_claims"), "completion": c.get("completion", {}).get("completed"),
                  **({"under_claims": c["under_claims_report_only"]["false_failure"]} if "under_claims_report_only" in c else {})}
    return out

def main():
    res_f = OUT / "comparison.json"
    res = json.loads(res_f.read_text()) if res_f.exists() else {"cases": {}}
    only = sys.argv[1:]
    for proto, app, contract, ui in CASES:
        key = f"{proto}:{app}"
        if only and key not in only: continue
        for ver in ("v1", "v2"):
            print("==", key, ver, flush=True); t0 = time.time()
            rc, j, err = cli("qualify", "--contract", contract, "--ui", ui, "--seed", "20261107", "--scripted-version", ver)
            rec = {"rc": rc, "seconds": round(time.time() - t0, 1), "stderr_tail": err if rc else ""}
            if j and j.get("bundle"):
                rc2, vj, _ = cli("contract", "verify-bundle", j["bundle"])
                rec.update({"bundle": Path(j["bundle"]).name, "qualification_status": j.get("qualification_status"), "harness_valid": all((j.get("harness_validity") or {}).values()),
                            "verify_ok": (vj or {}).get("ok"), "verify_scripted_policy_version": (vj or {}).get("scripted_policy_version"),
                            "verify_matches_current_file": (vj or {}).get("matches_current_file"), "candidates": summarise(j["bundle"])})
            res["cases"].setdefault(key, {})[ver] = rec; res_f.write_text(json.dumps(res, indent=1, default=str))
            print(json.dumps({k: rec.get(k) for k in ("rc", "qualification_status", "verify_ok", "verify_scripted_policy_version")}), flush=True)
    print("COMPARE DONE", flush=True)

if __name__ == "__main__":
    main()
