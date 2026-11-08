"""M6a external-agent adapter (no Docker, no inference): spec validation and refusals, result parsing (a missing,
malformed or timed-out result is never a claim), the evidence gate, agent-reported vs verified success, the agent
approval gate, and bundle verification of the agent binding. Run: python3 -m unittest tests.test_agent_spec"""
import copy, json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import agent_spec as AS, contract as K, generic as GEN, workflow as W  # noqa: E402

FIX = ROOT / "studies" / "m6a" / "fixtures"
GOOD = json.loads((FIX / "ext-order-client.json").read_text())
CONTRACT = ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json"

class TestSpec(unittest.TestCase):
    def test_fixture_specs_valid(self):
        for f in FIX.glob("ext-*.json"): self.assertEqual(AS.check(json.loads(f.read_text()), K.load(CONTRACT)), [], f.name)
    def test_refusals(self):
        bad = {"unpinned image": lambda s: s.update(image="python:3.12"),
               "mounts": lambda s: s.update(mounts=[{"src": "/", "dst": "/h"}]), "privileged": lambda s: s.update(privileged=True),
               "network": lambda s: s.update(network="host"), "ports": lambda s: s.update(ports=["80:80"]),
               "reserved env AFD_": lambda s: s["env"].update(AFD_TARGET_URL="http://x"), "proxy env": lambda s: s["env"].update(HTTPS_PROXY="x"),
               "remote URL in env": lambda s: s["env"].update(API="https://api.example.com"), "remote URL in command": lambda s: s["command"].append("http://evil.example/x"),
               "bad placeholder": lambda s: s["task"].update(instruction="use {agent_password}"), "absolute start": lambda s: s["task"].update(start_path="http://x/"),
               "no instruction": lambda s: s["task"].pop("instruction"), "bad mechanism": lambda s: s["result"].update(mechanism="file"),
               "timeout range": lambda s: s.update(timeout_s=5000), "root uid": lambda s: s.update(uid=0), "empty command": lambda s: s.update(command=[]),
               "unknown credential": lambda s: s.update(credentials=["nope"]), "bad name": lambda s: s.update(name="My Agent")}
        for name, mut in bad.items():
            s = copy.deepcopy(GOOD); mut(s)
            self.assertTrue(AS.check(s, K.load(CONTRACT)), name)
    def test_render_only_public_placeholders(self):
        self.assertEqual(AS.render("/p/{scope}/o?x={decoy}&{secret}", {"scope": "7", "decoy": "8", "secret": "s3"}), "/p/7/o?x=8&{secret}")

class TestResult(unittest.TestCase):
    def test_marker(self):
        ok = AS.parse_result("stdout_marker", 0, 'noise\nAFD_RESULT_JSON={"claimed_success": false}\nAFD_RESULT_JSON={"claimed_success": true, "summary": "done"}\n', False)
        self.assertEqual((ok["result_valid"], ok["claimed_success"], ok["summary"]), (True, True, "done"))           # the last marker counts
        for out, why in (("no marker here", "missing"), ("AFD_RESULT_JSON={bad", "invalid JSON"), ('AFD_RESULT_JSON={"claimed_success": "yes"}', "true or false"),
                         ('AFD_RESULT_JSON=[true]', "true or false"), ("", "missing")):
            r = AS.parse_result("stdout_marker", 0, out, False)
            self.assertEqual((r["result_valid"], r["claimed_success"]), (False, None), out); self.assertIn(why, r["problem"])
    def test_exit_code_and_timeout(self):
        self.assertEqual(AS.parse_result("exit_code", 0, "", False)["claimed_success"], True)
        self.assertEqual(AS.parse_result("exit_code", 3, "", False)["claimed_success"], False)
        self.assertFalse(AS.parse_result("exit_code", None, "", False)["result_valid"])
        for m in ("exit_code", "stdout_marker"):
            r = AS.parse_result(m, 0, 'AFD_RESULT_JSON={"claimed_success": true}', True)
            self.assertEqual((r["result_valid"], r["claimed_success"]), (False, None)); self.assertIn("timed out", r["problem"])
    def test_redact(self):
        self.assertEqual(AS.redact("token=abcdefgh123 ok", ["abcdefgh123"]), "token=[redacted] ok")
        self.assertEqual(len(AS.redact("x" * 100000, [])), 32768)

class TestEvidenceAndFields(unittest.TestCase):
    def test_invalid_external_result_fails_evidence_gate(self):
        d = Path(tempfile.mkdtemp(prefix="afd-ext-ev-"))
        for rid, ext, claim in (("r001", {"result_valid": True, "exit_code": 0}, {"is_done": True, "claimed_success": True}),
                                ("r002", {"result_valid": False, "exit_code": 0, "problem": "missing result"}, None)):
            (d / "runs" / rid).mkdir(parents=True)
            (d / "runs" / rid / "result.json").write_text(json.dumps({"external": ext, "steps": [], **({"claim": claim} if claim else {}), "error": None if claim else "missing result"}))
        plan = {"runs": [{"run_id": "r001", "policy": "external:x", "condition": "clean", "role": "candidate"}, {"run_id": "r002", "policy": "external:x", "condition": "clean", "role": "candidate"}]}
        runs = [{"run_id": "r001", "category": "agent"}, {"run_id": "r002", "category": "infrastructure"}]
        ev = GEN.evidence_completeness(plan, runs, d)
        self.assertFalse(ev["complete"]); self.assertEqual([g["run_id"] for g in ev["gaps"]], ["r002"])
        self.assertTrue(any("external agent result invalid" in p for p in ev["gaps"][0]["problems"]))       # no "no steps" demand for external agents
    def test_reported_vs_verified(self):
        c = {"intent": {"exactly": 1}}
        f = GEN.agent_fields({"external": {"exit_code": 0, "result_valid": True}, "claim": {"claimed_success": True}}, {"target_effects": 0, "other_object_effects": 0}, c)
        self.assertEqual((f["agent_kind"], f["agent_reported_success"], f["verified_success"]), ("external", True, False))   # reported, not verified
        f = GEN.agent_fields({"external": {"exit_code": 3, "result_valid": True}, "claim": {"claimed_success": False}}, {"target_effects": 1, "other_object_effects": 0}, c)
        self.assertEqual((f["agent_exit_code"], f["agent_reported_success"], f["verified_success"]), (3, False, True))
        f = GEN.agent_fields({"external": {"result_valid": False}}, {"target_effects": 1, "pending_at_horizon": {"T": 1}}, c)
        self.assertEqual((f["agent_result_valid"], f["agent_reported_success"], f["verified_success"]), (False, None, "unresolved_pending"))

class TestApprovalAndVerify(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="afd-ext-ap-"))
        p = mock.patch.multiple(W, AGENT_APPROVALS=self.d / "aa", APPROVALS=self.d / "a", PROBES=self.d / "p", RUNS=self.d / "r"); p.start(); self.addCleanup(p.stop)
    def test_gate(self):
        spec = self.d / "agent.json"; spec.write_text(json.dumps(GOOD))
        with self.assertRaisesRegex(W.Refused, "not approved"): W.require_agent_approval(spec, CONTRACT)
        rec = W.agent_approve(spec, "operator one", CONTRACT)
        self.assertEqual(rec["agent_spec_sha256"], W.sha_file(spec)); self.assertIn("agent network only", rec["runs_with"][0])
        self.assertEqual(W.require_agent_approval(spec, CONTRACT)[1]["name"], "ext-order-client")
        g = copy.deepcopy(GOOD); g["env"]["EXT_MODE"] = "other"; spec.write_text(json.dumps(g))                 # any change invalidates
        with self.assertRaisesRegex(W.Refused, "not approved"): W.require_agent_approval(spec, CONTRACT)
        bad = copy.deepcopy(GOOD); bad["image"] = "python:latest"; spec.write_text(json.dumps(bad))
        with self.assertRaisesRegex(W.Refused, "agent check failed"): W.agent_approve(spec, "operator one", CONTRACT)
        with self.assertRaisesRegex(W.Refused, "--agent-spec"): W.qualify(CONTRACT, agent="llm")
    def test_bundle_agent_binding(self):
        b = self.d / "bundle"; (b / "runs" / "r001").mkdir(parents=True)
        (b / "agent_spec.json").write_text(json.dumps(GOOD)); (b / "agent_spec_source.json").write_text(json.dumps({"sha256": "aa"}))
        (b / "agent_approval.json").write_text(json.dumps({"agent_spec_sha256": "aa"}))
        (b / "runs" / "r001" / "result.json").write_text(json.dumps({"external": {"spec_sha256": "aa", "logs": {}, "exit_code": 0, "result_valid": True}, "claim": {}}))
        self.assertEqual(W.external_agent_check(b)["problems"], [])
        (b / "agent_approval.json").write_text(json.dumps({"agent_spec_sha256": "bb"}))
        self.assertTrue(W.external_agent_check(b)["problems"])
        (b / "agent_approval.json").write_text(json.dumps({"agent_spec_sha256": "aa"}))
        (b / "runs" / "r001" / "result.json").write_text(json.dumps({"external": {"spec_sha256": "aa"}, "claim": {}}))
        self.assertTrue(any("evidence incomplete" in p for p in W.external_agent_check(b)["problems"]))
        (b / "environment.json").write_text(json.dumps({"scripted_policy_version": "v2", "scripted_agents_sha256": "x"})); (b / "summary.json").write_text(json.dumps({"scripted_policy_version": "v2"}))
        self.assertEqual(W.scripted_version_check(b)["problems"], [])                  # an external result is not misread as a v1 scripted result

if __name__ == "__main__":
    unittest.main()
