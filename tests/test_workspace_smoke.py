"""Pass/fail logic of deploy/vm/workspace_smoke.py (no Docker). Run: python3 -m unittest tests.test_workspace_smoke"""
import importlib.util, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("workspace_smoke", ROOT / "deploy" / "vm" / "workspace_smoke.py")
WS = importlib.util.module_from_spec(spec); spec.loader.exec_module(WS)

FILES = [("probe.py", False, 10, "ab" * 32)]
EXT = {"result_valid": True, "exit_code": 0, "timed_out": False}
GOOD = {"cwd_listing": ["probe.py"], "self_sha256": "ab" * 32, "write:/afd/workspace/probe.py": "denied:13", "write:/afd/workspace/new-file": "denied:30",
        "chmod": "denied:30", "mounts": ["/afd/workspace ro,nosuid,nodev,relatime"]}

class TestEvaluate(unittest.TestCase):
    def ok(self, ext=EXT, probe=GOOD, staged=(), left=()):
        return {k: v["pass"] for k, v in WS.evaluate(ext, probe, FILES, list(staged), list(left)).items()}
    def test_good(self): self.assertTrue(all(self.ok().values()))
    def test_each_failure_detected(self):
        cases = {"W1_result": dict(ext={**EXT, "result_valid": False}), "W2_content": dict(probe={**GOOD, "cwd_listing": ["probe.py", "x"]}),
                 "W3_read_only": dict(probe={**GOOD, "write:/afd/workspace/new-file": "WRITABLE"}),
                 "W4_mounts": dict(probe={**GOOD, "mounts": ["/afd/workspace rw,relatime"]}), "W5_cleanup": dict(staged=["stage-1"])}
        for check, kw in cases.items(): self.assertFalse(self.ok(**kw)[check], check)
        self.assertFalse(self.ok(probe={**GOOD, "self_sha256": "00" * 32})["W2_content"])
        self.assertFalse(self.ok(probe={**GOOD, "mounts": GOOD["mounts"] + ["/opt/afd-next ro"]})["W4_mounts"])     # any other host path mounted
        self.assertFalse(self.ok(probe={})["W3_read_only"])                                                         # no observation is not a pass
        self.assertFalse(self.ok(left=["afd-x-net"])["W5_cleanup"])

if __name__ == "__main__":
    unittest.main()
