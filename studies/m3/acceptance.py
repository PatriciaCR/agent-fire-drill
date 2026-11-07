"""M3 acceptance ($0, scripted only): drives the real CLI (bin/afd, subprocess) through check -> approve -> probe ->
qualify -> verify-bundle for the four study apps, plus the doc-route fixture (probe must fail with "declared action
never observed", qualify must be refused) and a tampered-bundle check. Writes studies/m3/results/acceptance.json.
Operator state (approvals, probes, bundles) lives in var/workflow/ (gitignored)."""
import json, shutil, subprocess, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
AFD = [sys.executable, str(ROOT / "bin" / "afd")]
OUT = ROOT / "studies" / "m3" / "results"; OUT.mkdir(parents=True, exist_ok=True)
CASES = [(a, f"studies/compat/contracts/{a}-v1.1.json", f"studies/compat/apps/{a}.json") for a in ("vikunja", "gitea", "listmonk", "woocommerce")] \
      + [("vikunja-docroute", "studies/m3/fixtures/vikunja-docroute-v1.1.json", "studies/compat/apps/vikunja.json")]

def cli(*args, timeout=3600):
    t0 = time.time(); r = subprocess.run([*AFD, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    try: j = json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError: j = {"unparsed_stdout_tail": r.stdout[-800:]}
    return {"rc": r.returncode, "seconds": round(time.time() - t0, 1), "out": j, "stderr_tail": r.stderr.strip()[-600:]}

def main():
    only = sys.argv[1:]
    res_f = OUT / "acceptance.json"
    res = json.loads(res_f.read_text()) if res_f.exists() else {"apps": {}}
    for name, contract, ui in CASES:
        if only and name not in only: continue
        print("==", name, flush=True); r = {}
        r["check"] = cli("contract", "check", contract, "--ui", ui)
        r["approve"] = cli("contract", "approve", contract, "--approver", "M3 acceptance - local operator", "--ui", ui)
        r["probe"] = cli("contract", "probe", contract, "--ui", ui)
        r["qualify"] = cli("qualify", "--contract", contract, "--ui", ui, "--seed", "20261107")
        b = ((r["qualify"].get("out") or {}).get("bundle"))
        if b:
            r["verify_bundle"] = cli("contract", "verify-bundle", b)
            if name == "vikunja":                                   # tamper test on a copy (the original bundle is untouched)
                t = Path(tempfile.mkdtemp(prefix="afd-m3-tamper-")) / "b"; shutil.copytree(b, t)
                lines = (t / "runs.jsonl").read_text().splitlines(); rec = json.loads(lines[0]); rec["verified_by_object"] = {rec["target"]: 1}; rec["inferred_by_object"] = {rec["target"]: 1}
                (t / "runs.jsonl").write_text("\n".join([json.dumps(rec)] + lines[1:]) + "\n")
                r["tampered_copy_manifest"] = cli("contract", "verify-bundle", str(t))
                subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from afd import bundle as B; B.seal(sys.argv[2])", str(ROOT), str(t)], check=True)
                r["tampered_copy_resealed"] = cli("contract", "verify-bundle", str(t))
                shutil.rmtree(t.parent)
        res["apps"][name] = r; res_f.write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps({k: (v["rc"], (v.get("out") or {}).get("status") or (v.get("out") or {}).get("qualification_status") or (v.get("out") or {}).get("ok") or v["stderr_tail"][-200:])
                          for k, v in r.items()}, default=str), flush=True)
    print("ACCEPTANCE DONE", flush=True)

if __name__ == "__main__":
    main()
