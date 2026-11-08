"""deploy/vm/p3_check.py, the read-only P3 bundle verifier, against the real local rehearsal bundle and resealed
mutations of it (no Docker). Run: python3 -m unittest tests.test_p3_check"""
import contextlib, importlib.util, io, json, shutil, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import bundle as B  # noqa: E402
spec = importlib.util.spec_from_file_location("p3_check", ROOT / "deploy" / "vm" / "p3_check.py"); PC = importlib.util.module_from_spec(spec); spec.loader.exec_module(PC)
REH = ROOT / "studies" / "p3" / "bundles" / "llm-20261108T090833Z-89e729"

class TestP3Check(unittest.TestCase):
    def setUp(self): self.tmp = Path(tempfile.mkdtemp(prefix="afd-p3check-")); self.addCleanup(shutil.rmtree, self.tmp, True)
    def check(self, d, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): rc = PC.main([str(d), *args])
        return rc, out.getvalue()
    def mutated(self, fn, name="runs.jsonl"):
        d = self.tmp / REH.name; shutil.copytree(REH, d); (d / "MANIFEST.sha256").unlink()
        fn(d); B.seal(d); return d
    def edit_json(self, name, fn):
        def go(d): o = json.loads((d / name).read_text()); fn(o); (d / name).write_text(json.dumps(o))
        return go
    def test_real_rehearsal_passes(self):
        rc, out = self.check(REH, "--stage", "rehearsal", "--expect-mode", "rootful"); self.assertEqual(rc, 0, out); self.assertIn("P3 CHECK PASS", out)
    def test_wrong_stage_or_mode_fails(self):
        self.assertEqual(self.check(REH, "--stage", "preflight", "--expect-mode", "rootful")[0], 1)
        self.assertEqual(self.check(REH, "--stage", "rehearsal")[0], 1)                                       # default expects rootless
    def test_tampered_bundle_fails_on_integrity_first(self):
        d = self.tmp / "t"; shutil.copytree(REH, d); (d / "summary.json").write_text("{}")
        rc, out = self.check(d, "--stage", "rehearsal", "--expect-mode", "rootful")
        self.assertEqual(rc, 1); self.assertIn("FAILED    bundle integrity", out); self.assertNotIn("CONFIRMED mode", out)
    def test_each_kind_of_bad_evidence_fails(self):
        def wopen(o): o["P2_worker_isolation_in_container"]["closed"]["proxy-agent-side:8080"] = "OPEN"
        def bopen(o): o["P3_browser_isolation_in_container"]["result"]["open"]["extra"] = "OPEN"
        def drop(o): o["P2_worker_isolation_in_container"]["closed"] = {k: v for k, v in o["P2_worker_isolation_in_container"]["closed"].items() if not k.startswith("vm-public-ip")}
        def root(o): o["P2_worker_isolation_in_container"]["uid"] = 0
        def lim(o): o["P3_browser_isolation_in_container"]["result"]["memory_max"] = "max"
        def gw(o): o["P6_gateway_token_caps_scope_revocation"]["statuses"]["model_not_allowed"] = 200
        def call(o): o["P6_gateway_token_caps_scope_revocation"]["ledger"]["calls"] = 1
        def td(o): o["teardown"]["live_tokens"] = ["abc"]
        for fn, want in ((wopen, "worker network"), (bopen, "browser network"), (drop, "worker network"), (root, "worker non-root"), (lim, "browser cgroup"),
                         (gw, "gateway authorization"), (call, "zero upstream"), (td, "teardown")):
            rc, out = self.check(self.mutated(self.edit_json("preflight.json", fn)), "--stage", "rehearsal", "--expect-mode", "rootful")
            self.assertEqual(rc, 1, want); self.assertIn(f"FAILED    {want}", out); shutil.rmtree(self.tmp / REH.name)
    def test_rehearsal_episode_with_an_upstream_call_fails(self):
        def go(d):
            rs = [json.loads(l) for l in (d / "runs.jsonl").read_text().splitlines()]; rs[1]["spend"]["calls"] = 1
            (d / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rs))
        rc, out = self.check(self.mutated(go), "--stage", "rehearsal", "--expect-mode", "rootful")
        self.assertEqual(rc, 1); self.assertIn("FAILED    rehearsal episodes", out); self.assertIn("FAILED    zero upstream model calls (bundle)", out)
    def test_ledger_reread_detects_upstream_rows(self):
        led = self.tmp / "ledger"; led.mkdir()
        (led / "llme1-x.jsonl").write_text(json.dumps({"cost_usd": 0.002, "status": 200}) + "\n")
        rc, out = self.check(REH, "--stage", "rehearsal", "--expect-mode", "rootful", "--ledger-dir", str(led))
        self.assertEqual(rc, 1); self.assertIn("FAILED    zero upstream model calls (gateway ledger", out)
    def test_live_token_detected(self):
        tok = self.tmp / "tokens"; tok.mkdir(); (tok / ("a" * 64 + ".json")).write_text(json.dumps({"job_id": "llme2-20261108-abc"}))
        rc, out = self.check(REH, "--stage", "rehearsal", "--expect-mode", "rootful", "--token-dir", str(tok))
        self.assertEqual(rc, 1); self.assertIn("FAILED    no live llm-job token", out)

if __name__ == "__main__":
    unittest.main()
