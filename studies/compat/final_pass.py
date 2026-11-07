"""Final compatibility evaluation pass on frozen code (the same proxy / contract runner / engine for every app).
Runs, per required app: core pipeline (afd.generic.run: self-test G1-G5, contract-v0 protocol, verdict, sealed bundle),
measurements (studies/compat/measure.py: D3, D4, D8, AM-2, AM-5) and real-browser checks (studies/compat/browser.py) for
lost_ack / fabricated / delayed_commit where the action has a UI. Then the reuse-proof regression and the unit tests.
Writes studies/compat/results/final/summary.json. $0 (scripted policies, scripted browser, no inference)."""
import json, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from afd import bundle as B, firewall as F, gateway as G, generic as GEN  # noqa: E402

APPS = ["vikunja", "gitea", "listmonk", "woocommerce"]
OUT = ROOT / "studies" / "compat" / "results" / "final"; OUT.mkdir(parents=True, exist_ok=True)
CODE = ["afd/faults/proxy.py", "afd/contract.py", "afd/generic.py", "afd/agents/scripted.py", "afd/qualify.py", "afd/bundle.py", "protocols/contract-v0.json"]

def main():
    F.install(); G.up()
    summary = {"started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "code_sha256": {f: B.sha256_file(ROOT / f) for f in CODE}, "apps": {}}
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    summary["git_head_at_start"] = commit
    for app in APPS:
        cfg = json.loads((ROOT / "studies" / "compat" / "apps" / f"{app}.json").read_text())
        rec = {"contract": cfg["contract"]}
        print(f"== {app}: core pipeline", flush=True)
        try:
            b, dig = GEN.run(ROOT / cfg["contract"], ["scripted_trust_response", "scripted_verify_no_resubmit"], ROOT / "var" / "compat", seed=20261107)
            s = json.loads((b / "summary.json").read_text()); st = json.loads((b / "selftest.json").read_text()); v = s["verdict"]
            rec["core"] = {"bundle": b.name, "digest": dig, "bundle_verifies": B.verify(b)["ok"], "selftest_pass": st["pass"],
                           "selftest": {k: x for k, x in st["checks"].items()}, "harness_valid": v["harness_validity"]["valid"],
                           "harness_checks": v["harness_validity"]["checks"], "positive_control": v["harness_validity"]["positive_control"],
                           "run_categories": v["run_categories"], "candidates": v["candidates"]}
            runs = [json.loads(l) for l in (b / "runs.jsonl").read_text().splitlines() if l.strip()]
            rec["core"]["am1_agreement_all_runs"] = all(r.get("proxy_inferred") == r.get("verified") for r in runs) if runs else None
            rec["core"]["am3_other_writes_unclassified"] = sum(1 for r in runs for w in r.get("other_writes", []) if w["class"] == "unclassified")
        except Exception as e:
            rec["core"] = {"error": f"{type(e).__name__}: {str(e)[:400]}"}
        print(f"== {app}: measurements", flush=True)
        r = subprocess.run([sys.executable, str(ROOT / "studies" / "compat" / "measure.py"), app], capture_output=True, text=True, timeout=2400)
        mfile = ROOT / "studies" / "compat" / "results" / f"{app}-measure.json"
        rec["measure"] = json.loads(mfile.read_text()) if r.returncode == 0 and mfile.exists() else {"error": r.stderr[-600:]}
        rec["browser"] = {}
        if cfg.get("browser"):
            for cond in ("lost_ack", "fabricated", "delayed_commit"):
                print(f"== {app}: browser {cond}", flush=True)
                r = subprocess.run([sys.executable, str(ROOT / "studies" / "compat" / "browser.py"), app, cond], capture_output=True, text=True, timeout=1200)
                bfile = ROOT / "studies" / "compat" / "results" / f"{app}-browser" / f"{cond}.json"
                if r.returncode == 0 and bfile.exists():
                    d = json.loads(bfile.read_text())
                    rec["browser"][cond] = {"verified": d["verified"], "proxy_inferred": d["proxy_inferred"],
                                            "proxy_requests": [(x["status_to_client"], x["intervention"], x["outcome"]) for x in d["proxy_requests"]],
                                            "oracle_series": [x["verified"] for x in d.get("oracle_series", [])], "screenshot": d["steps"][-1].get("png"),
                                            "unclassified_other_writes": [w for w in d.get("other_writes", []) if w["class"] == "unclassified"]}
                else:
                    rec["browser"][cond] = {"error": r.stderr[-600:]}
        else:
            rec["browser"] = {"not_applicable": cfg.get("browser_note")}
        summary["apps"][app] = rec
        (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print("== reuse proof regression", flush=True)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "reuse_proof.py")], capture_output=True, text=True, timeout=3600)
    summary["reuse_proof"] = {"returncode": r.returncode, "criteria": [l[:120] for l in r.stdout.splitlines() if l.startswith(("PASS", "FAIL"))]}
    print("== unit tests", flush=True)
    r = subprocess.run([sys.executable, "-m", "unittest", "tests.test_gateway", "tests.test_engine", "tests.test_web", "tests.test_deploy", "tests.test_listeners",
                        "tests.test_ui_model", "tests.test_validate_bundle", "tests.test_contract"], cwd=ROOT, capture_output=True, text=True, timeout=900)
    summary["unit_tests"] = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "?"
    summary["code_sha256_at_end"] = {f: B.sha256_file(ROOT / f) for f in CODE}
    summary["code_unchanged_during_pass"] = summary["code_sha256_at_end"] == summary["code_sha256"]
    summary["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print("FINAL PASS DONE", flush=True)

if __name__ == "__main__":
    main()
