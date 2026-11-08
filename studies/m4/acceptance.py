"""M4 / contract-v2 acceptance ($0, scripted only), driving the real CLI (bin/afd):
  WooCommerce, Vikunja, Listmonk v1.2: check -> approve WITHOUT --accept-destructive-controls (must be refused) -> approve
  with it -> probe -> qualify -> verify-bundle; a tampered-classification copy must fail verification;
  fixture: Listmonk falsely declaring read-back -> probe FAIL (contradiction), qualify refused.
Writes studies/m4/results/acceptance.json. Operator state in var/workflow/ (gitignored)."""
import json, shutil, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
AFD = [sys.executable, str(ROOT / "bin" / "afd")]
OUT = ROOT / "studies" / "m4" / "results"; OUT.mkdir(parents=True, exist_ok=True)
C = "studies/compat/contracts"; A = "studies/compat/apps"
CASES = [("woocommerce", f"{C}/woocommerce-v1.2.json", f"{A}/woocommerce.json"), ("vikunja", f"{C}/vikunja-v1.2.json", f"{A}/vikunja.json"),
         ("listmonk", f"{C}/listmonk-v1.2.json", f"{A}/listmonk.json"),
         ("listmonk-readback-declared", "studies/m4/fixtures/listmonk-readback-declared-v1.2.json", f"{A}/listmonk.json")]

def cli(*args, timeout=5400):
    t0 = time.time(); r = subprocess.run([*AFD, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    try: j = json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError: j = {"unparsed_stdout_tail": r.stdout[-800:]}
    return {"rc": r.returncode, "seconds": round(time.time() - t0, 1), "out": j, "stderr_tail": r.stderr.strip()[-900:]}

def main():
    import os
    only = sys.argv[1:]; res_f = Path(os.environ["AFD_ACCEPT_OUT"]) if os.environ.get("AFD_ACCEPT_OUT") else OUT / "acceptance.json"   # override: re-runs never overwrite the M4 record
    res = json.loads(res_f.read_text()) if res_f.exists() else {"apps": {}}
    for name, contract, ui in CASES:
        if only and name not in only: continue
        print("==", name, flush=True); r = {}
        r["check"] = cli("contract", "check", contract, "--ui", ui)
        r["approve_without_destructive_flag"] = cli("contract", "approve", contract, "--approver", "M4 acceptance - local operator", "--ui", ui)
        r["approve"] = cli("contract", "approve", contract, "--approver", "M4 acceptance - local operator", "--ui", ui, "--accept-destructive-controls")
        r["probe"] = cli("contract", "probe", contract, "--ui", ui)
        import os
        sv = ["--scripted-version", os.environ["AFD_SCRIPTED_VERSION"]] if os.environ.get("AFD_SCRIPTED_VERSION") else []      # regression re-runs: reproduce v1-era records
        r["qualify"] = cli("qualify", "--contract", contract, "--ui", ui, "--seed", "20261107", *sv)
        b = (r["qualify"].get("out") or {}).get("bundle")
        if b:
            r["verify_bundle"] = cli("contract", "verify-bundle", b)
            t = Path(tempfile.mkdtemp(prefix="afd-m4-tamper-")) / "b"; shutil.copytree(b, t)
            lines = (t / "runs.jsonl").read_text().splitlines(); recs = [json.loads(l) for l in lines]
            i = next((k for k, x in enumerate(recs) if x.get("protection")), 0)
            if recs[i].get("protection"): recs[i]["protection"][0]["class"] = "DEDUPLICATED" if recs[i]["protection"][0]["class"] != "DEDUPLICATED" else "REJECTED"
            else: recs[i]["fidelity"] = "UNRESOLVED"
            (t / "runs.jsonl").write_text("".join(json.dumps(x) + "\n" for x in recs))
            subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from afd import bundle as B; B.seal(sys.argv[2])", str(ROOT), str(t)], check=True)
            r["tampered_classification_resealed"] = cli("contract", "verify-bundle", str(t)); shutil.rmtree(t.parent)
        res["apps"][name] = r; res_f.write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps({k: (v["rc"], (v.get("out") or {}).get("status") or (v.get("out") or {}).get("qualification_status") or (v.get("out") or {}).get("ok") or v["stderr_tail"][-160:])
                          for k, v in r.items()}, default=str), flush=True)
    print("M4 ACCEPTANCE DONE", flush=True)

if __name__ == "__main__":
    main()
