"""Tests for deploy/vm/validate-bundle.py, run as a subprocess against a real local smoke bundle that is re-shaped into
VM form (rootless mode, VM containment evidence, matching commit) and re-sealed. Run: python3 -m unittest tests.test_validate_bundle"""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import bundle as B  # noqa: E402
SRC = next((p for p in sorted((ROOT / "var" / "qualifications").glob("smoke-*")) if p.is_dir() and (p / "runs.jsonl").exists()
            and isinstance(json.loads((p / "runs.jsonl").read_text().splitlines()[0]).get("requests"), list)), None)
COMMIT = "e5d611e37f27e1107780a60450569193e152d4a3"
FAKE_TOKEN_NAME = "f" * 64 + ".json"

@unittest.skipUnless(SRC, "needs a local smoke bundle with a request log (var/qualifications)")
class TestValidator(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp()); self.res, self.state, self.acc = self.t / "results", self.t / "state", self.t / "acc"
        for d in (self.res / "_requests", self.state / "ledger", self.state / "tokens", self.acc): d.mkdir(parents=True)
        self.b = self.res / SRC.name; shutil.copytree(SRC, self.b)
        s = json.loads((self.b / "summary.json").read_text()); self.job = s["job"]
        ev = {"environment": "vm", "pass": True, "commit": COMMIT, "summary": {"PASS": 51, "FAIL": 0, "SKIPPED": 0}, "checks": {}}
        (self.acc / "latest.json").write_text(json.dumps(ev))
        shutil.rmtree(self.b / "containment", ignore_errors=True); (self.b / "containment").mkdir()
        (self.b / "containment" / "latest.json").write_text(json.dumps(ev))
        s["containment_evidence"] = {"file": "latest.json", "sha256": B.sha256_file(self.acc / "latest.json"), "pass": True, "environment": "vm"}
        (self.b / "summary.json").write_text(json.dumps(s))
        env = json.loads((self.b / "environment.json").read_text()); env.update(mode="rootless", afd_commit=COMMIT)
        (self.b / "environment.json").write_text(json.dumps(env))
        B.seal(self.b)
        shutil.copy(self.b / "ledger.jsonl", self.state / "ledger" / f"{self.job}.jsonl")
        (self.res / "_requests" / "s-20261107000000-abcd1234.json").write_text(json.dumps({"id": "s-20261107000000-abcd1234", "state": "done", "bundle": SRC.name}))
    def tearDown(self): shutil.rmtree(self.t)
    def run_v(self, arg=None):
        env = {**os.environ, "AFD_ENV_FILE": "/nonexistent", "AFD_RESULTS": str(self.res), "AFD_STATE": str(self.state), "AFD_ACCEPTANCE": str(self.acc)}
        r = subprocess.run([sys.executable, "-I", str(ROOT / "deploy" / "vm" / "validate-bundle.py"), *([arg] if arg else []), "--no-live"], env=env, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr
    def test_clean_bundle_passes_by_bundle_request_and_default(self):
        for arg in (SRC.name, "s-20261107000000-abcd1234", None):
            rc, out = self.run_v(arg); self.assertEqual(rc, 0, out); self.assertIn("PASS  verdict.json recomputes", out); self.assertNotRegex(out, r"(?m)^FAIL ")
    def test_tampering_is_caught(self):
        def expect(name):
            rc, out = self.run_v(SRC.name); self.assertEqual(rc, 1, out); self.assertIn(f"FAIL  {name}", out); return out
        p = self.b / "runs.jsonl"; orig = p.read_bytes(); p.write_bytes(orig.replace(b'"backend_effects": 2', b'"backend_effects": 1', 1))
        expect("manifest: every file matches its sha256"); p.write_bytes(orig)
        v = json.loads((self.b / "verdict.json").read_text()); v["run"]["duplicate"] = False; (self.b / "verdict.json").write_text(json.dumps(v)); B.seal(self.b)
        expect("verdict.json recomputes exactly"); shutil.copy(SRC / "verdict.json", self.b / "verdict.json"); B.seal(self.b)
        led = self.state / "ledger" / f"{self.job}.jsonl"; led.write_text(led.read_text() + '{"cost_usd": 0.0}\n')
        expect("bundle ledger is byte-identical"); shutil.copy(self.b / "ledger.jsonl", led)
        (self.state / "tokens" / FAKE_TOKEN_NAME).write_text(json.dumps({"job_id": self.job, "budget_usd": 0.05, "secret_marker": "TOKEN-CONTENT-MUST-NOT-PRINT"}))
        out = expect("job token revoked"); self.assertNotIn(FAKE_TOKEN_NAME[:16], out); self.assertNotIn("TOKEN-CONTENT-MUST-NOT-PRINT", out)
        (self.state / "tokens" / FAKE_TOKEN_NAME).unlink()
        env = json.loads((self.b / "environment.json").read_text()); env["afd_commit"] = "0" * 40; (self.b / "environment.json").write_text(json.dumps(env)); B.seal(self.b)
        expect("evidence commit = commit the bundle ran on")
    def test_unknown_id_exits_nonzero(self):
        rc, out = self.run_v("s-20990101000000-ffffffff"); self.assertNotEqual(rc, 0)

if __name__ == "__main__":
    unittest.main()
