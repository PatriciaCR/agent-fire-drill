"""M5 / contract-v3 acceptance ($0, scripted policy v2), driving the real CLI (bin/afd):
  controlled fixture (genuinely late backend commits) and WooCommerce, Vikunja, Listmonk v1.3 (pre-commit failure; no
  late hook, so late conditions NOT APPLICABLE): check -> approve (destructive controls) -> probe -> qualify with
  scripted_trust_response, scripted_verify_no_resubmit, scripted_verify_then_retry -> verify-bundle; a tampered v3
  classification must fail verification; v1 cannot run the v2-only policy. Writes studies/m5/results/acceptance.json."""
import json, shutil, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
AFD = [sys.executable, str(ROOT / "bin" / "afd")]
OUT = ROOT / "studies" / "m5" / "results"; OUT.mkdir(parents=True, exist_ok=True)
C, A = "studies/compat/contracts", "studies/compat/apps"
CANDS = "scripted_trust_response,scripted_verify_no_resubmit,scripted_verify_then_retry"
CASES = [("fixture-latecommit", "studies/m5/fixtures/latecommit-v1.3.json", "studies/m5/fixtures/latecommit-ui.json")] + \
        [(a, f"{C}/{a}-v1.3.json", f"{A}/{a}.json") for a in ("woocommerce", "vikunja", "listmonk")]

def cli(*args, timeout=7200):
    t0 = time.time(); r = subprocess.run([*AFD, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    try: j = json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError: j = {"unparsed_stdout_tail": r.stdout[-800:]}
    return {"rc": r.returncode, "seconds": round(time.time() - t0, 1), "out": j, "stderr_tail": r.stderr.strip()[-900:]}

def main():
    only = sys.argv[1:]; res_f = OUT / "acceptance.json"
    res = json.loads(res_f.read_text()) if res_f.exists() else {"apps": {}}
    for name, contract, ui in CASES:
        if only and name not in only: continue
        print("==", name, flush=True); r = {}
        r["check"] = cli("contract", "check", contract, "--ui", ui)
        r["approve"] = cli("contract", "approve", contract, "--approver", "M5 acceptance - local operator", "--ui", ui, "--accept-destructive-controls")
        r["probe"] = cli("contract", "probe", contract, "--ui", ui)
        if name == "fixture-latecommit":
            r["v1_refuses_v2_only_policy"] = cli("qualify", "--contract", contract, "--ui", ui, "--candidates", CANDS, "--scripted-version", "v1")
        r["qualify"] = cli("qualify", "--contract", contract, "--ui", ui, "--seed", "20261107", "--candidates", CANDS)
        b = (r["qualify"].get("out") or {}).get("bundle")
        if b:
            r["verify_bundle"] = cli("contract", "verify-bundle", b)
            if name == "fixture-latecommit":
                t = Path(tempfile.mkdtemp(prefix="afd-m5-tamper-")) / "b"; shutil.copytree(b, t)
                recs = [json.loads(l) for l in (t / "runs.jsonl").read_text().splitlines() if l.strip()]
                i = next(k for k, x in enumerate(recs) if x.get("pending_at_horizon") or x["condition"].startswith("late"))
                recs[i]["run_effect_safety"] = "PASS" if recs[i]["run_effect_safety"] != "PASS" else "FAIL"
                (t / "runs.jsonl").write_text("".join(json.dumps(x) + "\n" for x in recs))
                subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from afd import bundle as B; B.seal(sys.argv[2])", str(ROOT), str(t)], check=True)
                r["tampered_v3_classification_resealed"] = cli("contract", "verify-bundle", str(t)); shutil.rmtree(t.parent)
        res["apps"][name] = r; res_f.write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps({k: (v["rc"], (v.get("out") or {}).get("status") or (v.get("out") or {}).get("qualification_status") or (v.get("out") or {}).get("ok") or v["stderr_tail"][-160:])
                          for k, v in r.items()}, default=str), flush=True)
    print("M5 ACCEPTANCE DONE", flush=True)

if __name__ == "__main__":
    main()
