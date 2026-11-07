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
    def test_goal_has_no_site_name(self):
        self.assertNotIn("BreakingWeb", self.W.goal("do it"))

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
