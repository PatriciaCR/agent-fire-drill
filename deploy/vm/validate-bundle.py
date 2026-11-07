#!/usr/bin/env python3
"""Read-only validation of an Agent Fire Drill evidence bundle on the VM (root). Works for smoke and qualification bundles.

Usage:  sudo python3 -I /opt/afd/deploy/vm/validate-bundle.py [BUNDLE_ID | REQUEST_ID] [--no-live]
        no argument  -> the most recent request (smoke or qualification) that produced a bundle
        --no-live    -> skip the live Docker check (used by the unit tests; never needed on the VM)

Checks (PASS / FAIL; INFO lines are not part of the result):
  manifest      every file matches MANIFEST.sha256; nothing missing or unlisted
  per run       backend effects = initial - final = effect-ledger entries; ledger agrees; request log ties every commit
                to a request (bundles that predate the request log: INFO)
  ledger        the bundle's gateway ledger is byte-identical to the live gateway ledger; spend = sum of rows
  verdict       verdict.json recomputes exactly from protocol/plan/runs/self-test/teardown (afd/qualify.py:verdict)
  tokens        the job's token is revoked (no live token file references the job)
  teardown      the teardown report is clean, and no container or network labelled with the job still exists (live)
  containment   the bundle's copy of the containment evidence matches its recorded sha256; it is VM evidence that passed;
                and it was produced on the same commit the bundle ran on
Exit status 0 only if every check passes. Reads no secrets; prints no token values or token file names."""
import glob, hashlib, json, os, subprocess, sys
from pathlib import Path

def env_file(p="/etc/afd/afd.env"):
    try: return {k: v for k, v in (l.split("=", 1) for l in Path(p).read_text().splitlines() if "=" in l and not l.lstrip().startswith("#"))}
    except OSError: return {}
CFG = {**env_file(os.environ.get("AFD_ENV_FILE", "/etc/afd/afd.env")), **{k: v for k, v in os.environ.items() if k.startswith("AFD_")}}
RESULTS = Path(CFG.get("AFD_RESULTS", "/var/lib/afd/results"))
STATE = Path(CFG.get("AFD_STATE", "/var/lib/afd"))
ACCEPTANCE = Path(CFG.get("AFD_ACCEPTANCE", "/var/lib/afd/acceptance"))
ROOT = Path(__file__).resolve().parent.parent.parent
RESULT = {"ok": True}

def check(name, cond, detail=""):
    RESULT["ok"] &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  · {detail}" if detail != "" else ""), flush=True)
def info(name, detail=""): print(f"INFO  {name}" + (f"  · {detail}" if detail != "" else ""), flush=True)
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def resolve(arg):
    reqdir = RESULTS / "_requests"
    if not arg:
        reqs = [p for p in sorted(reqdir.glob("*.json"), key=os.path.getmtime) if json.loads(p.read_text()).get("bundle")]
        if not reqs: sys.exit("no request with a bundle found")
        arg = json.loads(reqs[-1].read_text())["id"]
    if (reqdir / f"{arg}.json").exists():
        req = json.loads((reqdir / f"{arg}.json").read_text())
        if req.get("state") != "done" or not req.get("bundle"): sys.exit(f"request {arg} has no bundle (state: {req.get('state')})")
        print(f"request {arg} -> bundle {req['bundle']}"); return RESULTS / req["bundle"]
    if (RESULTS / arg / "summary.json").exists(): return RESULTS / arg
    sys.exit(f"unknown bundle or request id: {arg}")

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]; live = "--no-live" not in sys.argv
    B = resolve(args[0] if args else None)
    j = lambda f: json.loads((B / f).read_text())
    s, td, env = j("summary.json"), j("teardown.json"), j("environment.json")
    runs = [json.loads(l) for l in (B / "runs.jsonl").read_text().splitlines() if l.strip()]
    print(f"bundle {B.name} · profile {s['profile']} · job {s['job']} · {len(runs)} run(s)\n")
    # ---- manifest ----
    lines = [l for l in (B / "MANIFEST.sha256").read_text().splitlines() if l.strip()]
    listed = {l.split("  ", 1)[1]: l.split("  ", 1)[0] for l in lines}
    present = {str(p.relative_to(B)) for p in B.rglob("*") if p.is_file()} - {"MANIFEST.sha256"}
    bad = [f for f, h in listed.items() if f in present and sha(B / f) != h]
    check("manifest: every file matches its sha256", not bad, f"{len(listed)} files" + (f"; modified: {bad[:5]}" if bad else ""))
    check("manifest: no missing or unlisted files", set(listed) == present,
          "" if set(listed) == present else f"missing {sorted(set(listed) - present)[:5]} unlisted {sorted(present - set(listed))[:5]}")
    # ---- per run ----
    scored = [r for r in runs if r.get("category") == "agent"]
    info("run categories", {c: sum(r.get("category") == c for r in runs) for c in ("agent", "budget_stop", "infrastructure")})
    math = [r["run_id"] for r in scored if not (r["backend_effects"] == r["initial"] - r["final"] == r["ledger_effects"] == len(r["effect_ledger"]) and r["ledger_agrees"])]
    check("every scored run: backend effects = initial - final = effect-ledger entries", not math, f"{len(scored)} scored" + (f"; mismatched {math}" if math else ""))
    with_log = [r for r in scored if isinstance(r.get("requests"), list)]
    tied = [r["run_id"] for r in with_log if sorted(x["effect_index"] for x in r["requests"] if x["effect_index"] is not None) != list(range(len(r["effect_ledger"])))]
    if with_log: check("every commit is tied to a recorded request", not tied, f"{len(with_log)} runs with a request log" + (f"; untied {tied}" if tied else ""))
    if len(with_log) < len(scored): info("runs without a request log (predate the recorder)", len(scored) - len(with_log))
    for r in runs[:5] if s["profile"] == "smoke" else []:
        info(f"run {r['run_id']} outcome", f"{r['policy']} / {r['condition']} · effects {r.get('backend_effects')} · duplicate {r.get('duplicate')} · "
             f"requests {[(x['seq'], x['status_to_client'], x['intervention'], x['outcome']) for x in r.get('requests') or []]}")
    # ---- ledger and spend ----
    led = STATE / "ledger" / f"{s['job']}.jsonl"
    check("bundle ledger is byte-identical to the live gateway ledger", led.exists() and sha(B / "ledger.jsonl") == sha(led))
    rows = [json.loads(l) for l in (B / "ledger.jsonl").read_text().splitlines() if l.strip()]
    total = round(sum(x["cost_usd"] for x in rows), 6)
    check("spend in summary = sum of ledger rows", abs(total - s["spend"]["cost_usd"]) < 1e-6, f"${total} of ${s['budget_usd']} budget; {s['spend']['calls']} calls; "
          f"{s['spend'].get('budget_refusals', 0)} budget refusals")
    # ---- verdict recomputation ----
    try:
        sys.path.insert(0, str(ROOT)); from afd import qualify as Q
        again = Q.verdict(j("protocol.json"), j("plan.json"), runs, j("selftest.json"), td)
        check("verdict.json recomputes exactly from the bundle's inputs", json.loads(json.dumps(again)) == j("verdict.json"))
    except Exception as e:
        check("verdict.json recomputes exactly from the bundle's inputs", False, f"{type(e).__name__}: {e}")
    # ---- tokens (never print token values or file names) ----
    refs = 0
    for p in glob.glob(str(STATE / "tokens" / "*.json")):
        try: refs += json.loads(Path(p).read_text()).get("job_id") == s["job"]
        except (OSError, ValueError): pass
    check("job token revoked", td.get("token_revoked") is True and refs == 0, f"live token files referencing this job: {refs}")
    info("live token files on the system (any job)", len(glob.glob(str(STATE / "tokens" / "*.json"))))
    # ---- teardown ----
    clean = all(v is True for k, v in td.items() if k.endswith(("_removed", "_revoked"))) and not td.get("leftover_containers") and not td.get("leftover_networks")
    check("teardown report clean", clean, json.dumps(td))
    if live:
        orch = [str(ROOT / "deploy" / "vm" / "afdctl"), "orch", "docker"]
        c = subprocess.run(orch + ["ps", "-a", "--filter", f"label=afd.job={s['job']}", "--format", "{{.Names}}"], capture_output=True, text=True)
        n = subprocess.run(orch + ["network", "ls", "--filter", f"label=afd.job={s['job']}", "--format", "{{.Name}}"], capture_output=True, text=True)
        check("no container or network of this job exists now (live)", c.returncode == 0 and n.returncode == 0 and not c.stdout.split() and not n.stdout.split(),
              f"containers={c.stdout.split()} networks={n.stdout.split()}" + (f" errors={c.stderr.strip()[-120:]} {n.stderr.strip()[-120:]}" if c.returncode or n.returncode else ""))
    else: info("live Docker check skipped (--no-live)")
    # ---- containment reference ----
    ref = s.get("containment_evidence") or {}
    copy = B / "containment" / str(ref.get("file"))
    check("bundle's containment copy matches its recorded sha256", copy.is_file() and sha(copy) == ref.get("sha256"), str(ref.get("sha256", ""))[:16])
    ev = json.loads(copy.read_text()) if copy.is_file() else {}
    check("containment evidence is from the VM and passed", ev.get("environment") == "vm" and ev.get("pass") is True,
          f"environment {ev.get('environment')} · pass {ev.get('pass')} · {ev.get('summary')}")
    check("evidence commit = commit the bundle ran on", bool(env.get("afd_commit")) and ev.get("commit") == env.get("afd_commit"),
          f"bundle {str(env.get('afd_commit'))[:12]} · evidence {str(ev.get('commit'))[:12]}")
    cur = ACCEPTANCE / "latest.json"
    info("bundle's evidence is the current VM evidence", cur.exists() and sha(cur) == ref.get("sha256"))
    check("bundle ran in rootless mode", env.get("mode") == "rootless", env.get("mode"))
    print(f"\nBUNDLE {B.name}: {'PASS' if RESULT['ok'] else 'FAIL'}")
    sys.exit(0 if RESULT["ok"] else 1)

if __name__ == "__main__":
    main()
