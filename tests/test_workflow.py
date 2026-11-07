"""M3 Test Your Workflow (no Docker, no inference): `afd contract check` refusals, the approval gate (unapproved /
changed contract / changed images / changed UI script), qualify refusals, and the CLI's exit codes.
Run: python3 -m unittest tests.test_workflow"""
import copy, json, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import workflow as W  # noqa: E402

BASE = json.loads((ROOT / "studies" / "compat" / "contracts" / "vikunja-v1.1.json").read_text())
UI = ROOT / "studies" / "compat" / "apps" / "vikunja.json"

class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="afd-wf-test-"))
        p = mock.patch.multiple(W, APPROVALS=self.d / "approvals", PROBES=self.d / "probes", RUNS=self.d / "runs"); p.start(); self.addCleanup(p.stop)
    def write(self, c, name="c.json"):
        f = self.d / name; f.write_text(json.dumps(c)); return f
    def problems(self, mutate):
        c = copy.deepcopy(BASE); mutate(c); return W.check(self.write(c))

class TestCheck(Tmp):
    def test_study_contracts_pass(self):
        for app in ("vikunja", "gitea", "listmonk", "woocommerce"):
            r = W.check(ROOT / "studies" / "compat" / "contracts" / f"{app}-v1.1.json", ROOT / "studies" / "compat" / "apps" / f"{app}.json")
            self.assertTrue(r["pass"], (app, r["problems"])); self.assertTrue(all(W.PINNED.match(i) for i in r["images"]))
    def test_unsafe_contracts_are_rejected(self):
        app = lambda c: next(s for s in c["services"] if s["name"] == "app")
        cases = {
            "remote URL in env": lambda c: app(c)["env"].update(X_UPSTREAM="https://api.example.com/v1"),
            "remote URL in a setup step header": lambda c: c["setup_once"].append({"service": "app", "method": "GET", "path": "/", "headers": {"Referer": "http://evil.example.net/"}}),
            "remote service url key": lambda c: app(c).update(url="https://example.com"),
            "root uid": lambda c: app(c).update(uid=0),
            "privileged sysctl": lambda c: app(c).update(sysctls={"kernel.core_pattern": "x"}),
            "privileged flag": lambda c: app(c).update(privileged=True),
            "added capability": lambda c: app(c).update(cap_add=["NET_ADMIN"]),
            "published port": lambda c: app(c).update(ports=["8080:8080"]),
            "unpinned image": lambda c: app(c).update(image="vikunja/vikunja:latest"),
            "unpinned oracle image": lambda c: c["objects"]["oracle"].update(image="postgres:17"),
            "mount escape": lambda c: app(c).update(mounts=[{"src": "../../etc", "dst": "/x"}]),
            "writable root": lambda c: app(c).update(writable=["/"]),
            "missing oracle": lambda c: c.pop("oracle"),
            "missing per-object oracle": lambda c: c["objects"].pop("oracle"),
            "missing target": lambda c: c["objects"].pop("target"),
            "no objects section (v1)": lambda c: (c.update(contract="afd-workflow-contract/1"), c.pop("objects")),
            "no attestation": lambda c: c["authorization"].pop("attestation"),
            "oracle independence undeclared": lambda c: c["objects"]["oracle"].pop("independence"),
            "writing oracle": lambda c: c["objects"]["oracle"].update(command=["-c", "DELETE FROM tasks; SELECT project_id, count(*) FROM tasks GROUP BY 1"]),
            "oracle through the proxy": lambda c: c.update(oracle={"kind": "http", "service": "app", "method": "GET", "path": "{public_url}/api", "mode": "items", "value": "", "independence": "L1"}),
        }
        for name, mutate in cases.items():
            r = self.problems(mutate)
            self.assertFalse(r["pass"], name); self.assertTrue(r["problems"], name)
        self.assertGreaterEqual(len(cases), 8)
    def test_ui_script_must_stay_on_the_proxy(self):
        f = self.d / "ui.json"; f.write_text(json.dumps({"steps": [["nav", "https://example.com/"]]}))
        self.assertFalse(W.check(ROOT / "studies" / "compat" / "contracts" / "vikunja-v1.1.json", f)["pass"])
        f.write_text(json.dumps({"steps": [["exec", "rm -rf /"]]}))
        self.assertFalse(W.check(ROOT / "studies" / "compat" / "contracts" / "vikunja-v1.1.json", f)["pass"])

class TestApprovalGate(Tmp):
    def test_unapproved_changed_contract_images_and_ui_are_refused(self):
        f = self.write(BASE)
        with self.assertRaisesRegex(W.Refused, "not approved"): W.require_approval(f, UI)
        with self.assertRaisesRegex(W.Refused, "approver"): W.approve(f, "", UI)
        rec = W.approve(f, "operator one", UI)
        self.assertEqual(rec["contract_sha256"], W.sha_file(f)); self.assertEqual(rec["images"], W.check(f, UI)["images"])
        self.assertEqual(W.require_approval(f, UI)[0]["contract_sha256"], rec["contract_sha256"])
        with self.assertRaisesRegex(W.Refused, "UI script differs"): W.require_approval(f, None)
        ui2 = self.d / "ui2.json"; ui2.write_text(json.dumps({"steps": [["nav", "http://afd-proxy:8080/"]]}))
        with self.assertRaisesRegex(W.Refused, "UI script differs"): W.require_approval(f, ui2)
        c = copy.deepcopy(BASE); c["intent"]["note"] = "edited"; self.write(c)                      # any byte change
        with self.assertRaisesRegex(W.Refused, "contract changed since approval"): W.require_approval(f, UI)
        c = copy.deepcopy(BASE); next(s for s in c["services"] if s["name"] == "app")["image"] = "vikunja/vikunja@sha256:" + "0" * 64; self.write(c)
        with self.assertRaisesRegex(W.Refused, "contract changed since approval"): W.require_approval(f, UI)
    def test_approval_record_images_tampered(self):
        f = self.write(BASE); rec = W.approve(f, "operator one", UI)
        p = W.APPROVALS / f"{rec['contract_sha256']}.json"; r = json.loads(p.read_text()); r["images"] = r["images"][:1]; p.write_text(json.dumps(r))
        with self.assertRaisesRegex(W.Refused, "image set differs"): W.require_approval(f, UI)
    def test_unsafe_contract_cannot_be_approved(self):
        c = copy.deepcopy(BASE); next(s for s in c["services"] if s["name"] == "app")["image"] = "vikunja/vikunja:latest"
        with self.assertRaisesRegex(W.Refused, "check failed"): W.approve(self.write(c), "operator one", UI)

class TestQualifyRefusals(Tmp):
    def setUp(self):
        super().setUp(); p = mock.patch.object(W, "require_local_images", lambda imgs: None); p.start(); self.addCleanup(p.stop)
        self.f = self.write(BASE)
    def test_llm_agent_refused(self):
        with self.assertRaisesRegex(W.Refused, "only --agent scripted"): W.qualify(self.f, UI, agent="llm")
    def test_positive_control_is_not_a_candidate_and_unknown_policies_refused(self):
        with self.assertRaises(ValueError): W.qualify(self.f, UI, candidates=["positive_control_blind_retry"])
    def test_unapproved_unprobed_and_failed_probe_refused(self):
        with self.assertRaisesRegex(W.Refused, "not approved"): W.qualify(self.f, UI)
        ap = W.approve(self.f, "operator one", UI)
        with self.assertRaisesRegex(W.Refused, "no probe"): W.qualify(self.f, UI)
        rec = json.loads((W.APPROVALS / f"{ap['contract_sha256']}.json").read_text())
        W.PROBES.mkdir(parents=True)
        pf = W.PROBES / f"{ap['contract_sha256']}.json"
        pf.write_text(json.dumps({"approval_record_sha256": W.sha_obj(rec), "ui_sha256": ap["ui_sha256"], "pass": False, "status": "FAIL",
                                  "ui_traffic": {"reason": "declared action never observed"}, "limitations": []}))
        with self.assertRaisesRegex(W.Refused, "declared action never observed"): W.qualify(self.f, UI)
        pf.write_text(json.dumps({"approval_record_sha256": "stale", "ui_sha256": ap["ui_sha256"], "pass": True}))
        with self.assertRaisesRegex(W.Refused, "predates"): W.qualify(self.f, UI)

class TestCLI(Tmp):
    def test_exit_codes(self):
        sys.path.insert(0, str(ROOT / "bin"))
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("afd_cli", str(ROOT / "bin" / "afd"))
        spec = importlib.util.spec_from_loader("afd_cli", loader); cli = importlib.util.module_from_spec(spec); loader.exec_module(cli)
        good, bad = self.write(BASE, "good.json"), self.write({**BASE, "contract": "afd-workflow-contract/9"}, "bad.json")
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["contract", "check", str(good), "--ui", str(UI)]), 0)
            self.assertEqual(cli.main(["contract", "check", str(bad)]), 1)
            self.assertEqual(cli.main(["qualify", "--contract", str(good), "--agent", "llm"]), 1)

if __name__ == "__main__":
    unittest.main()
