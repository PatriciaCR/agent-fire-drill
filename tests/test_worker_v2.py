"""M2 browser worker v2 evidence (no Docker, no browser, no inference): allow-listed element fields, password inputs,
and redaction of the job token and secret-like strings. Run: python3 -m unittest tests.test_worker_v2"""
import importlib.util, json, os, sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def load_worker(token):
    os.environ["AFD_JOB_TOKEN"] = token
    spec = importlib.util.spec_from_file_location("browser_worker_v2", ROOT / "afd" / "agents" / "browser_worker_v2.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

class Node:
    def __init__(self, tag, attrs, text): self.tag_name, self.attributes, self._t = tag, attrs, text
    def get_all_children_text(self): return self._t

class TestElementEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.W = load_worker("tok-" + "a1" * 20)
    @classmethod
    def tearDownClass(cls): os.environ.pop("AFD_JOB_TOKEN", None)
    def test_allow_listed_fields_only(self):
        e = self.W.element(17, Node("BUTTON", {"aria-label": "Request refill for X", "value": "hidden-value", "href": "/a?session=1", "onclick": "x()", "class": "btn", "type": "button"}, "Request   Refill"))
        self.assertEqual(e, {"index": 17, "tag": "button", "type": "button", "aria_label": "Request refill for X", "text": "Request Refill"})
    def test_password_input_carries_no_text_or_name(self):
        e = self.W.element(3, Node("input", {"type": "password", "name": "password", "placeholder": "Password"}, "typed-secret"))
        self.assertEqual(e, {"index": 3, "tag": "input", "type": "password", "placeholder": "Password", "text": None})
    def test_redaction(self):
        tok = os.environ["AFD_JOB_TOKEN"]
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlc2lnbmF0dXJl"
        s = self.W.clean(f"token {tok} jwt {jwt} hex {'ab' * 20} ok", 400)
        self.assertNotIn(tok, s); self.assertNotIn(jwt, s); self.assertNotIn("ab" * 20, s); self.assertIn("ok", s)
        self.assertEqual(len(self.W.clean("word " * 100)), 80)                        # truncated
        self.assertEqual(self.W.clean("x" * 500), "[redacted]")                    # one long unbroken token is treated as secret-like
    def test_long_actions_stay_valid_json(self):
        """Regression (M2 e1): truncating serialised JSON crashed the export; strings are now cut individually."""
        actions = [{"input": {"index": 788, "text": "Pay invoice 1042 " * 400, "clear": True}}, {"click": {"index": 799}}] * 30
        out = self.W.clean_tree(actions)
        json.loads(json.dumps(out))
        self.assertEqual(out[1], {"click": {"index": 799}}); self.assertLessEqual(len(out[0]["input"]["text"]), 600)
        self.assertEqual(out[0]["input"]["index"], 788); self.assertIs(out[0]["input"]["clear"], True)
        self.assertLessEqual(len(out), 40)
        tok = os.environ["AFD_JOB_TOKEN"]
        self.assertNotIn(tok, json.dumps(self.W.clean_tree([{"input": {"text": f"x {tok} y"}}])))
    def test_goal_has_no_site_name(self):
        self.assertNotIn("BreakingWeb", self.W.goal("do it"))

class _A:
    def __init__(self, d): self.d = d
    def model_dump(self, exclude_none=True): return self.d
class _NS:
    def __init__(self, **k): self.__dict__.update(k)

class TestExportLargeRecords(unittest.TestCase):
    """Regression for the M2 e1 crash, at scale: export() of a synthetic browser-use history with many long, nested,
    non-ASCII action records must produce valid, bounded JSON that keeps every step and redacts secrets."""
    @classmethod
    def setUpClass(cls): cls.tok = "tok-" + "c3" * 20; cls.W = load_worker(cls.tok)
    @classmethod
    def tearDownClass(cls): os.environ.pop("AFD_JOB_TOKEN", None)
    def history(self, n_steps=30, n_actions=40):
        big = ("Pay invoice 1042 — ünïcödé ✓ " * 4000)                         # ~100 KB
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": {"k": "deep"}}}}}}}}}}}
        hist = []
        for i in range(n_steps):
            acts = [_A({"input": {"index": 700 + j, "text": big + f" {self.tok}", "clear": True}}) if j % 3 == 0 else
                    _A({"click": {"index": 800 + j, "meta": deep, "n": 1.5, "flag": None}}) if j % 3 == 1 else
                    _A({"extract": {"query": ["x" * 5000] * 100}}) for j in range(n_actions)]
            mo = _NS(action=acts, thinking=big, evaluation_previous_goal=big, memory=big, next_goal=big)
            res = [_NS(extracted_content=big, error=None, is_done=False, success=None)]
            el = _NS(backend_node_id=812, node_name="BUTTON", attributes={"aria-label": "Add", "type": "button", "value": "v"}, ax_name="Add")
            pw = _NS(backend_node_id=5, node_name="INPUT", attributes={"type": "password", "name": "password"}, ax_name="secret-typed")
            st = _NS(url="http://afd-proxy:8080/projects/2/9?token=" + self.tok, interacted_element=[el, None, pw], screenshot_path=None)
            hist.append(_NS(model_output=mo, result=res, state=st, metadata=_NS(step_start_time=1.0 + i, step_end_time=2.0 + i)))
        return _NS(history=hist)
    def test_export_is_valid_bounded_and_complete(self):
        seen = {i: {"url_path": "/x", "elements": [{"index": 1, "tag": "a", "text": "t"}]} for i in range(1, 31)}
        steps, shots = self.W.export(self.history(), seen)
        blob = json.dumps({"steps": steps})
        self.assertEqual(len(json.loads(blob)["steps"]), 30)
        self.assertLess(len(blob), 30_000_000)                                          # within the worker's MAX_OUT
        self.assertNotIn(self.tok, blob)
        s0 = steps[0]
        self.assertEqual(len(s0["actions"]), 40)                                         # lists are capped at 40, every action kept here
        self.assertEqual(s0["actions"][1]["click"]["index"], 801); self.assertEqual(s0["actions"][1]["click"]["n"], 1.5)
        self.assertLessEqual(len(s0["actions"][0]["input"]["text"]), 600)
        self.assertEqual(len(s0["actions"][2]["extract"]["query"]), 40)
        self.assertEqual(s0["url_path"], "http://afd-proxy:8080/projects/2/9")          # query string dropped
        self.assertEqual(s0["elements_clicked"][0], {"backend_node_id": 812, "tag": "button", "type": "button", "aria_label": "Add", "accessible_name": "Add"})
        self.assertIsNone(s0["elements_clicked"][1])
        self.assertEqual(s0["elements_clicked"][2], {"backend_node_id": 5, "tag": "input", "type": "password", "accessible_name": None})
        self.assertEqual(s0["elements_seen"], seen[1])
        self.assertTrue(all(len(s["thinking"]) <= 2000 and len(s["memory"]) <= 800 for s in steps))
    def test_clean_tree_handles_depth_and_odd_types(self):
        out = self.W.clean_tree({"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": {"j": 1}}}}}}}}}, "o": object(), "t": (1, 2), 3: "k"})
        json.loads(json.dumps(out))
        self.assertIn("[truncated]", json.dumps(out)); self.assertEqual(out["t"], [1, 2]); self.assertEqual(out["3"], "k")

class TestNoHardcodingM2(unittest.TestCase):
    def test_new_generic_modules_have_no_app_identifiers(self):
        """Code only: module docstrings may name the protocols they relate to (e.g. the worker v1 it differs from)."""
        import ast
        ids = ["patient_portal", "refill", "lisinopril", "breakingweb", "medication", "vikunja", "gitea", "listmonk", "mailpit", "woocommerce", "wordpress", "postgres", "mariadb"]
        for f in ("afd/generic_llm.py", "afd/agents/browser_worker_v2.py"):
            tree = ast.parse((ROOT / f).read_text())
            if ast.get_docstring(tree): tree.body = tree.body[1:]
            text = ast.unparse(tree).lower(); self.assertEqual([i for i in ids if i in text], [], f)

if __name__ == "__main__":
    unittest.main()
