"""M6a external-agent adapter acceptance ($0; no inference). Contract: the M5 controlled fixture (approved and probed).
  A. adapter plumbing (engine, one run per variant): result mechanisms, malformed / missing / timed-out results,
     containment from the agent's network position, proxy interception of the agent's own write, oracle truth.
  B. real CLI: afd agent check/approve, afd qualify --agent-spec for the good client (full contract-v3 plan; the
     scripted positive controls validate the harness) and for the malformed client (must NOT be a qualification).
Writes studies/m6a/results/acceptance.json."""
import json, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from afd import contract as K, firewall as F, generic as GEN, workflow as W  # noqa: E402
FIX = ROOT / "studies" / "m6a" / "fixtures"; CONTRACT = ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json"
UI = ROOT / "studies" / "m5" / "fixtures" / "latecommit-ui.json"
OUT = ROOT / "studies" / "m6a" / "results"; OUT.mkdir(parents=True, exist_ok=True)
AFD = [sys.executable, str(ROOT / "bin" / "afd")]

def cli(*args, timeout=7200):
    t0 = time.time(); r = subprocess.run([*AFD, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    try: j = json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError: j = {"unparsed_stdout_tail": r.stdout[-800:]}
    return {"rc": r.returncode, "seconds": round(time.time() - t0, 1), "out": j, "stderr_tail": r.stderr.strip()[-700:]}

def part_a():
    F.install(); res = {}
    c = K.load(CONTRACT)
    with GEN.GenericJob(c, "m6a") as job:
        for name in ("ext-order-client", "ext-exitcode", "ext-malformed", "ext-missing", "ext-timeout", "ext-containment"):
            spec = json.loads((FIX / f"{name}.json").read_text())
            s = GEN.Scenario(job); job.external = {"spec": spec, "sha256": W.sha_file(FIX / f"{name}.json")}
            if name == "ext-order-client": job.arm(s.scope, "lost_ack", 6, s.target)                   # the agent's own write gets a transport failure
            t0 = time.time(); r = job.run_external(f"a-{name}", s); time.sleep(1)
            recs = [x for x in job.object_records(s.t0) if x["object"] == s.target]
            e = r["external"]
            res[name] = {"exit_code": e["exit_code"], "timed_out": e["timed_out"], "result_valid": e["result_valid"], "claimed_success": e["claimed_success"],
                         "problem": e["problem"], "summary": e["summary"], "has_claim": "claim" in r, "seconds": e["seconds"],
                         "proxy_records_on_target": [(x["status_to_client"], x["intervention"], x["outcome"]) for x in recs],
                         "verified_target_effects": s.object_effects().get(s.target, 0), "stdout_tail": e["logs"]["stdout_tail"][-300:]}
            print(name, json.dumps({k: res[name][k] for k in ("exit_code", "result_valid", "claimed_success", "problem", "proxy_records_on_target", "verified_target_effects")}), flush=True)
    res["_teardown"] = job.teardown_report
    return res

def part_b():
    r = {}
    for name in ("ext-order-client", "ext-malformed"):
        spec = str(FIX / f"{name}.json")
        r[name] = {"check": cli("agent", "check", spec, "--contract", str(CONTRACT)), "approve": cli("agent", "approve", spec, "--approver", "M6a acceptance - local operator", "--contract", str(CONTRACT))}
        r[name]["qualify"] = cli("qualify", "--contract", str(CONTRACT), "--ui", str(UI), "--agent-spec", spec, "--seed", "20261108")
        b = (r[name]["qualify"].get("out") or {}).get("bundle")
        if b: r[name]["verify_bundle"] = cli("contract", "verify-bundle", b)
        q = r[name]["qualify"].get("out") or {}
        print(name, json.dumps({"rc": r[name]["qualify"]["rc"], "status": q.get("qualification_status"), "external": q.get("external_agent"), "evidence": q.get("evidence_completeness"),
                                "verify": ((r[name].get("verify_bundle") or {}).get("out") or {}).get("ok")}), flush=True)
    unapproved = FIX.parent / "results" / "unapproved-agent.json"
    s = json.loads((FIX / "ext-order-client.json").read_text()); s["env"]["EXT_MODE"] = "good-but-changed"; unapproved.write_text(json.dumps(s))
    r["changed_spec_refused"] = cli("qualify", "--contract", str(CONTRACT), "--ui", str(UI), "--agent-spec", str(unapproved)); unapproved.unlink()
    print("changed spec:", r["changed_spec_refused"]["rc"], r["changed_spec_refused"]["stderr_tail"][-120:], flush=True)
    return r

if __name__ == "__main__":
    out = {"A_adapter_plumbing": part_a(), "B_cli": part_b()}
    (OUT / "acceptance.json").write_text(json.dumps(out, indent=1, default=str)); print("M6a ACCEPTANCE DONE", flush=True)
