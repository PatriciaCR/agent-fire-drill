"""Deployment smoke evaluations (deploy/vm/generic_smoke.py), no Docker: what counts as isolation, enforced limits,
a drilled and oracle-recorded write, and clean teardown; plus the limits fixture agent spec and the approval gate.
Run: python3 -m unittest tests.test_vm_smoke"""
import importlib.util, json, sys, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import agent_spec as AS, contract as K, workflow as W  # noqa: E402
spec = importlib.util.spec_from_file_location("generic_smoke", ROOT / "deploy" / "vm" / "generic_smoke.py")
SM = importlib.util.module_from_spec(spec); spec.loader.exec_module(SM)

OK_TCP = {"afd-proxy:8080": "OPEN", "app-container": "blocked:TimeoutError", "afd-inference-gateway": "blocked:OSError", "cloud-metadata": "blocked:OSError"}

class TestIsolation(unittest.TestCase):
    def test_only_proxy(self):
        self.assertTrue(SM.evaluate_isolation({"tcp": OK_TCP, "dns": {"app": "unresolvable:gaierror"}})["pass"])
    def test_leaks_fail(self):
        for k in ("app-container", "afd-inference-gateway", "cloud-metadata"):
            r = SM.evaluate_isolation({"tcp": {**OK_TCP, k: "OPEN"}, "dns": {}}); self.assertFalse(r["pass"], k); self.assertIn(k, r["problems"][0])
        self.assertFalse(SM.evaluate_isolation({"tcp": {**OK_TCP, "afd-proxy:8080": "blocked:OSError"}, "dns": {}})["pass"])      # proxy must be reachable
        self.assertFalse(SM.evaluate_isolation({"tcp": OK_TCP, "dns": {"app": "RESOLVES"}})["pass"])
        self.assertFalse(SM.evaluate_isolation({})["pass"])

def ext(report, exit_code=137, mem_lines=(8, 512, 1400), survived=False, valid=True):
    out = "AFD_RESULT_JSON=x\n" + "".join(f"MEM_MB {m}\n" for m in mem_lines) + ("MEM_SURVIVED_MB 3200\n" if survived else "")
    return {"result_valid": valid, "summary": json.dumps(report), "exit_code": exit_code, "logs": {"stdout_tail": out}, "problem": None if valid else "x"}

GOOD = {"uid": 10002, "rootfs_writable": False, "raw_socket": False, "setuid0": False, "tmp_written_mb": 512, "threads_started": 250}

class TestLimits(unittest.TestCase):
    def test_enforced(self):
        r = SM.evaluate_limits(ext(GOOD), 1536, 512, 256); self.assertTrue(r["pass"], r["problems"]); self.assertEqual(r["memory_reached_mb"], 1400)
    def test_violations(self):
        cases = {"root": {**GOOD, "uid": 0}, "rw root": {**GOOD, "rootfs_writable": True}, "raw": {**GOOD, "raw_socket": True}, "setuid": {**GOOD, "setuid0": True},
                 "tmp": {**GOOD, "tmp_written_mb": 800}, "pids": {**GOOD, "threads_started": 600}}
        for name, rep in cases.items(): self.assertFalse(SM.evaluate_limits(ext(rep), 1536, 512, 256)["pass"], name)
        self.assertFalse(SM.evaluate_limits(ext(GOOD, exit_code=0, survived=True), 1536, 512, 256)["pass"])     # memory limit not enforced
        self.assertFalse(SM.evaluate_limits(ext(GOOD, mem_lines=(8, 2400)), 1536, 512, 256)["pass"])           # killed too late
        self.assertFalse(SM.evaluate_limits(ext({}, valid=False), 1536, 512, 256)["pass"])
    def test_limits_spec_valid(self):
        s = json.loads((ROOT / "studies" / "m6a" / "fixtures" / "ext-smoke-limits.json").read_text())
        self.assertEqual(AS.check(s, K.load(ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json")), [])

class TestExecutionTeardown(unittest.TestCase):
    REC = [{"status_to_client": 503, "intervention": "lost_ack", "outcome": "committed"}]
    def test_execution(self):
        self.assertTrue(SM.evaluate_execution({"result_valid": True}, self.REC, 1, True)["pass"])
        self.assertFalse(SM.evaluate_execution({"result_valid": True}, [], 1, True)["pass"])                    # never reached the proxy
        self.assertFalse(SM.evaluate_execution({"result_valid": True}, [{**self.REC[0], "intervention": "none"}], 1, True)["pass"])
        self.assertFalse(SM.evaluate_execution({"result_valid": True}, self.REC, 0, True)["pass"])              # oracle saw nothing
        self.assertFalse(SM.evaluate_execution({"result_valid": True}, self.REC, 1, False)["pass"])
        self.assertFalse(SM.evaluate_execution({"result_valid": False, "problem": "x"}, self.REC, 1, True)["pass"])
    def test_teardown(self):
        good = {"app_removed": True, "proxy_removed": True, "token_revoked": True, "leftover_containers": [], "leftover_networks": []}
        self.assertTrue(SM.evaluate_teardown(good, [])["pass"])
        self.assertFalse(SM.evaluate_teardown({**good, "app_removed": False}, [])["pass"]); self.assertFalse(SM.evaluate_teardown(good, ["c1"])["pass"])
        self.assertFalse(SM.evaluate_teardown(None, [])["pass"])

class TestGate(unittest.TestCase):
    def test_unapproved_refused(self):
        with mock.patch.object(W, "require_approval", side_effect=W.Refused("contract not approved")), mock.patch("sys.stderr"):
            self.assertEqual(SM.main(), 2)

if __name__ == "__main__":
    unittest.main()
