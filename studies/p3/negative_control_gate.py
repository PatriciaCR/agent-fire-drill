"""P3 negative control ($0, local Docker): a worker container with a deliberate isolation break (it is also attached to
the agent network, so the proxy is reachable) must be refused by ISOLATION_GATE, in a gate-only run and in a real
evidence-only worker run, and the worker code must not start. Run: python3 -I studies/p3/negative_control_gate.py"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from afd import contract as K, gateway as G, generic_llm as GL, config as C
c = K.load(C.ROOT / "studies/compat/contracts/gitea-v1.1.json")
out = {}
with GL.LLMEpisodeJob(c, "negctl") as job:
    orig = job.harness_container
    def broken(nets, args, label, **kw):
        return orig(list(nets) + ([job.nets["agent"]] if label == "worker" else []), args, label, **kw)
    job.harness_container = broken
    tok, path = G.issue_token(f"{job.id}-neg", 0.000001)
    try:
        r, _, _ = job.run_worker("http://afd-proxy:8080/", token=tok, gate_only=True)
        out["gate_only"] = {"pass": r["isolation_gate"]["pass"], "violations": r["isolation_gate"]["violations"], "exit_code": r["isolation_gate"]["exit_code"]}
    finally: G.revoke(path)
    r, shots, _ = job.run_worker("http://afd-proxy:8080/", evidence_only=True)
    out["evidence_only_worker"] = {"error": r.get("error"), "worker_ran": "elements_seen" in r or "steps" in r, "violations": r["isolation_gate"]["violations"], "exit_code": r["isolation_gate"]["exit_code"]}
out["teardown_leftovers"] = job.teardown_report["leftover_containers"] + job.teardown_report["leftover_networks"]
print(json.dumps(out, indent=1))
