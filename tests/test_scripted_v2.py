"""M4.1 correction (no Docker, no inference): the historical v1 user-check traversal fails on array indices; v2 resolves
RFC 6901 pointers and separates an ATTEMPTED read-back from a CONFIRMED one; every bundle records which scripted-policy
version produced it. Run: python3 -m unittest tests.test_scripted_v2"""
import ast, importlib.util, json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import bundle as B, generic as GEN, workflow as W  # noqa: E402

spec = importlib.util.spec_from_file_location("scripted_v2", ROOT / "afd" / "agents" / "scripted_v2.py")
S2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(S2)          # import-safe: the agent runs only under __main__

def v1_confirmed(check, status, doc):
    """Run the UNCHANGED historical confirmed() from afd/agents/scripted.py (extracted by AST; nothing else executes)."""
    tree = ast.parse((ROOT / "afd" / "agents" / "scripted.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "confirmed")
    ns = {"C": {"check": check}, "step": lambda action, f: (status, doc)}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "scripted.py", "exec"), ns)
    return ns["confirmed"]()

VIK_DOC = [{"id": 1, "title": "Pay invoice 1042"}]
VIK_CHECK = {"method": "GET", "path": "/x", "value": "/0/title", "success_in": ["Pay invoice 1042"]}

class TestHistoricalFailure(unittest.TestCase):
    def test_v1_cannot_index_arrays(self):
        self.assertFalse(v1_confirmed(VIK_CHECK, 200, VIK_DOC))                          # the bug: the value IS there
        self.assertFalse(v1_confirmed({**VIK_CHECK, "value": "/items/0/title"}, 200, {"items": VIK_DOC}))
        sel = {**VIK_CHECK, "select": {"list": "/data/0/rows", "match": {"field": "id", "equals": "1"}}, "value": "/title"}
        self.assertFalse(v1_confirmed(sel, 200, {"data": [{"rows": VIK_DOC}]}))        # array step inside select.list too
    def test_v1_object_keys_work(self):
        self.assertTrue(v1_confirmed({"method": "GET", "path": "/c", "value": "/items_count", "success_in": [0]}, 200, {"items_count": 0}))
    def test_historical_file_is_unchanged(self):
        head = subprocess.run(["git", "-C", str(ROOT), "show", "896f360:afd/agents/scripted.py"], capture_output=True).stdout
        self.assertEqual(head, (ROOT / "afd" / "agents" / "scripted.py").read_bytes())

class TestPointer(unittest.TestCase):
    def test_resolution(self):
        doc = {"a": {"b": [10, {"c": "x"}]}, "list": [[1, 2], [3, [4, 5]]], "k/e~y": 7, "": "empty-key"}
        self.assertEqual(S2.resolve(doc, ""), doc)
        self.assertEqual(S2.resolve(doc, "/a/b/0"), 10); self.assertEqual(S2.resolve(doc, "/a/b/1/c"), "x")
        self.assertEqual(S2.resolve(doc, "/list/1/1/0"), 4)                                 # nested arrays
        self.assertEqual(S2.resolve(doc, "/k~1e~0y"), 7); self.assertEqual(S2.resolve(doc, "/"), "empty-key")
        self.assertEqual(S2.resolve(VIK_DOC, "/0/title"), "Pay invoice 1042")
    def test_missing_values(self):
        for ptr in ("/a/zz", "/a/b/5", "/list/9", "/a/b/0/deeper", "/a/b/-"):
            with self.assertRaises(S2.PointerError) as e: S2.resolve({"a": {"b": [10]}, "list": []}, ptr)
            self.assertEqual(e.exception.kind, "missing", ptr)
    def test_malformed_pointers(self):
        for ptr in ("0/title", "a", "/a/~2", "/list/01", "/list/x", "/list/-1", 5):
            with self.assertRaises(S2.PointerError) as e: S2.resolve({"a": {}, "list": [1, 2]}, ptr)
            self.assertEqual(e.exception.kind, "malformed_pointer", ptr)

class TestReadbackOutcome(unittest.TestCase):
    def test_attempted_is_not_confirmed(self):
        r = S2.evaluate_check(VIK_CHECK, 200, VIK_DOC); self.assertEqual((r["attempted"], r["lookup"], r["confirmed"]), (True, "found", True))
        r = S2.evaluate_check(VIK_CHECK, 200, []);      self.assertEqual((r["attempted"], r["lookup"], r["confirmed"]), (True, "missing", False))   # 200, empty list
        r = S2.evaluate_check(VIK_CHECK, 200, [{"title": "Something else"}]); self.assertEqual((r["lookup"], r["confirmed"]), ("found", False))   # found, wrong value
        r = S2.evaluate_check(VIK_CHECK, 404, None);    self.assertEqual((r["lookup"], r["confirmed"]), ("http_error", False))
        r = S2.evaluate_check(VIK_CHECK, 200, None);    self.assertEqual((r["lookup"], r["confirmed"]), ("no_json", False))
        r = S2.evaluate_check({**VIK_CHECK, "value": "0/title"}, 200, VIK_DOC); self.assertEqual((r["lookup"], r["confirmed"]), ("malformed_pointer", False))
    def test_baseline_and_select(self):
        k = {"method": "GET", "path": "/m", "select": {"list": "/items", "match": {"field": "id", "equals": "rx_1"}}, "value": "/refills_remaining", "baseline": 3}
        self.assertTrue(S2.evaluate_check(k, 200, {"items": [{"id": "rx_2", "refills_remaining": 6}, {"id": "rx_1", "refills_remaining": 2}]})["confirmed"])
        self.assertFalse(S2.evaluate_check(k, 200, {"items": [{"id": "rx_1", "refills_remaining": 3}]})["confirmed"])
        self.assertEqual(S2.evaluate_check(k, 200, {"items": [{"id": "rx_9"}]})["lookup"], "missing")
    def test_study_apps_check_pointers(self):
        """Each app's real contract check pointer against a representative response."""
        c = lambda app: json.loads((ROOT / "studies" / "compat" / "contracts" / f"{app}-v1.1.json").read_text())["user_check"]
        self.assertTrue(S2.evaluate_check(c("vikunja"), 200, VIK_DOC)["confirmed"])
        self.assertTrue(S2.evaluate_check(c("gitea"), 200, [{"title": "Refund order 1042"}])["confirmed"])
        lm = S2.evaluate_check(c("listmonk"), 200, {"data": {"results": [{"attribs": {}}]}})              # no afd_sent attribute: never confirms
        self.assertEqual((lm["lookup"], lm["confirmed"]), ("missing", False))
        self.assertTrue(S2.evaluate_check(c("woocommerce"), 200, {"items_count": 0})["confirmed"])
        for app, doc in (("vikunja", VIK_DOC), ("gitea", [{"title": "Refund order 1042"}])): self.assertFalse(v1_confirmed(c(app), 200, doc), app)
        self.assertTrue(v1_confirmed(c("woocommerce"), 200, {"items_count": 0}))

class TestVersionRecording(unittest.TestCase):
    def test_versions_and_defaults(self):
        self.assertEqual(set(GEN.SCRIPTED_SRC), {"v1", "v2"}); self.assertEqual(GEN.SCRIPTED_SRC["v1"], GEN.AGENT_SRC)
        import inspect
        self.assertEqual(inspect.signature(GEN.run).parameters["scripted_version"].default, "v1")         # library: reproduces frozen results
        self.assertEqual(inspect.signature(W.qualify).parameters["scripted_version"].default, "v2")       # new qualifications: corrected
    def test_qualify_passes_version_and_refuses_unknown(self):
        with self.assertRaisesRegex(W.Refused, "unknown --scripted-version"): W.qualify(ROOT / "x.json", scripted_version="v9")
    def bundle(self, env_ver, summ_ver, result_ver):
        d = Path(tempfile.mkdtemp(prefix="afd-sv-")) / "b"; (d / "runs" / "r001").mkdir(parents=True)
        (d / "environment.json").write_text(json.dumps({**({"scripted_policy_version": env_ver, "scripted_agents_sha256": B.sha256_file(GEN.SCRIPTED_SRC.get(env_ver, GEN.AGENT_SRC))} if env_ver else {})}))
        (d / "summary.json").write_text(json.dumps({**({"scripted_policy_version": summ_ver} if summ_ver else {})}))
        (d / "runs" / "r001" / "result.json").write_text(json.dumps({"claim": {}, **({"scripted_policy_version": result_ver} if result_ver else {})}))
        return d
    def test_bundle_version_check(self):
        r = W.scripted_version_check(self.bundle("v2", "v2", "v2")); self.assertEqual((r["scripted_policy_version"], r["problems"], r["matches_current_file"]), ("v2", [], True))
        r = W.scripted_version_check(self.bundle("v1", "v1", None)); self.assertEqual((r["scripted_policy_version"], r["problems"]), ("v1", []))
        self.assertTrue(W.scripted_version_check(self.bundle("v2", "v2", None))["problems"])                # a v1-produced result inside a v2 bundle
        self.assertTrue(W.scripted_version_check(self.bundle("v2", "v1", "v2"))["problems"])
        self.assertIn("unrecorded", W.scripted_version_check(self.bundle(None, None, None))["scripted_policy_version"])

if __name__ == "__main__":
    unittest.main()
