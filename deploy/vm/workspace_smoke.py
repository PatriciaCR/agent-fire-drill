#!/usr/bin/env python3
"""Workspace-mount smoke (M6c on the VM, rootless). $0, no inference, one short job on the approved M5 fixture contract.
Runs the approved ext-ws-probe agent FROM its hash-bound, read-only workspace and checks, from inside the container:
  W1 result      the agent produced a valid result through the declared mechanism
  W2 content     the mount holds exactly the approved file(s), byte-identical (probe.py hashes itself)
  W3 read-only   writing, creating and chmod inside /afd/workspace are all denied
  W4 mounts      /afd/workspace is the only host bind mount of interest, mounted ro
  W5 cleanup     the private staged copy is removed; no job containers or networks remain
Requires the operator approvals that `afd` itself requires (the M5 contract, ext-ws-probe). Writes nothing outside
$AFD_VAR/workflow (the staging dir is emptied again). Exit 0 iff all pass.
Run (VM): sudo -u afd-orch -H /opt/afd-next/deploy/vm/afd-ns python3 /opt/afd-next/deploy/vm/workspace_smoke.py"""
import hashlib, json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from afd import config as C, contract as K, dockerops as D, generic as GEN, workflow as W  # noqa: E402

CONTRACT = ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json"
UI = ROOT / "studies" / "m5" / "fixtures" / "latecommit-ui.json"
SPEC = ROOT / "studies" / "m6c" / "fixtures" / "ext-ws-probe.json"

def evaluate(ext, probe, approved_files, staged_left, leftovers):
    ch = {"W1_result": {"pass": bool(ext.get("result_valid")) and ext.get("exit_code") == 0 and not ext.get("timed_out")}}
    listing = probe.get("cwd_listing"); want = sorted(f[0] for f in approved_files)
    sha = {f[0]: f[3] for f in approved_files}.get("probe.py")
    ch["W2_content"] = {"pass": listing == want and probe.get("self_sha256") == sha, "listing": listing, "expected": want}
    denied = {k: v for k, v in probe.items() if k.startswith("write:") or k == "chmod"}
    ch["W3_read_only"] = {"pass": bool(denied) and all(str(v).startswith("denied:") for v in denied.values()), "observed": denied}
    m = probe.get("mounts") or []
    ws = [x for x in m if x.split()[0] == "/afd/workspace"]
    ch["W4_mounts"] = {"pass": len(ws) == 1 and "ro" in ws[0].split()[1].split(",") and all(x.split()[0].startswith("/afd/workspace") for x in m), "mounts": m}
    ch["W5_cleanup"] = {"pass": not staged_left and not leftovers, "staged_left": staged_left, "leftovers": leftovers}
    return ch

def main():
    t0 = time.time()
    try:
        W.require_approval(CONTRACT, UI)
        arec, spec = W.require_agent_approval(SPEC, CONTRACT)
        W.require_local_images(sorted({*W.images_of(K.load(CONTRACT)), spec["image"], GEN.RUNTIME_IMAGE}))
        root, entries, th = W.agent_workspace(spec)
    except W.Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr); return 2
    stage_parent = C.VAR / "workflow" / "ws-staging"
    with GEN.GenericJob(K.load(CONTRACT), "wssmoke") as job:
        job.external = {"spec": spec, "sha256": W.sha_file(SPEC), "approval": arec,
                        "workspace": {"root": str(root), "entries": entries, "tree_sha256": th, "stage_parent": str(stage_parent)}}
        r = job.run_external("ws-probe", GEN.Scenario(job))
    ext = r["external"]
    try: probe = json.loads(ext.get("summary") or "{}")
    except ValueError: probe = {}
    staged_left = sorted(p.name for p in stage_parent.iterdir()) if stage_parent.exists() else []
    leftovers = D.labelled("container", f"afd.job={job.id}") + D.labelled("network", f"afd.job={job.id}")
    checks = evaluate(ext, probe, entries, staged_left, leftovers)
    ok = all(c["pass"] for c in checks.values())
    print(json.dumps({"mode": C.MODE, "job": job.id, "seconds": round(time.time() - t0, 1), "workspace_tree_sha256": th, "approved_tree_sha256": arec.get("workspace_tree_sha256"),
                      "code_sha256": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in ("afd/generic.py", "afd/agent_spec.py", "deploy/vm/workspace_smoke.py")},
                      "checks": checks, "agent": {k: ext.get(k) for k in ("exit_code", "timed_out", "result_valid", "seconds", "workspace")},
                      "result": "PASS" if ok else "FAIL"}, indent=1, default=str))
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
