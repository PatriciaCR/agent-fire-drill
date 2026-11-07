"""Generalisation tests (no Docker, no inference): contract validation and its safety refusals, the generic fault proxy
against a fake upstream, the no-hardcoding rule, and plan() unchanged for refill-v1. Run: python3 -m unittest tests.test_contract"""
import json, os, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import contract as K, generic as GEN, qualify as Q  # noqa: E402

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class TestContracts(unittest.TestCase):
    def write(self, mutate):
        c = json.loads((ROOT / "contracts" / "expense-demo.json").read_text()); mutate(c)
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(c, f); f.close(); return f.name
    def test_shipped_contracts_are_valid(self):
        for f in sorted((ROOT / "contracts").glob("*.json")): K.load(f)
    def test_safety_refusals(self):
        bad = {"remote url": lambda c: c["app"].update(url="https://example.com"),
               "no attestation": lambda c: c["authorization"].pop("attestation"),
               "real data": lambda c: c["authorization"].update(synthetic_data=False),
               "outside sandbox": lambda c: c["authorization"].update(runs_in_sandbox=False),
               "mount escapes repo": lambda c: c["app"]["mounts"].append({"src": "../../etc", "dst": "/x"}),
               "bad scope location": lambda c: c["scope"].update({"in": "cookie"}),
               "bad intent": lambda c: c["intent"].update(exactly="one")}
        for why, m in bad.items():
            with self.assertRaises(K.ContractError, msg=why): K.load(self.write(m))
    def test_render_select_pointer(self):
        self.assertEqual(K.render({"a": ["/x/{item}?s={scope}"]}, scope="S", item="I"), {"a": ["/x/I?s=S"]})
        doc = {"items": [{"id": "a", "name": "Foo 1"}, {"id": "b", "name": "Bar"}]}
        self.assertEqual(K.select(doc, {"list": "/items", "match": {"field": "name", "prefix": "Bar"}})["id"], "b")
        self.assertEqual(K.select(doc, {"list": "/items", "match": {"field": "id", "equals": "a"}})["name"], "Foo 1")

class TestNoHardcoding(unittest.TestCase):
    def test_generic_modules_have_no_app_identifiers(self):
        ids = ["patient_portal", "refill", "lisinopril", "breakingweb", "medication", "expense", "reimburs", "session_id"]
        for f in ("afd/faults/proxy.py", "afd/contract.py", "afd/generic.py", "afd/agents/scripted.py"):
            text = (ROOT / f).read_text().lower(); self.assertEqual([i for i in ids if i in text], [], f)

class TestPlanUnchangedForRefill(unittest.TestCase):
    def test_refill_plan_identical_and_generic_registry(self):
        P, _ = Q.load_protocol()
        a = Q.plan(P, ["generic_guard", "cautious_no_resubmit"], "full", seed=5)
        self.assertEqual(len(a["runs"]), 46); self.assertEqual(Q.plan(P, ["generic_guard", "cautious_no_resubmit"], "full", seed=5), a)
        with self.assertRaises(ValueError): Q.plan(P, ["scripted_trust_response"], "full")          # refill still accepts only its presets
        G = K.protocol_for(K.load(ROOT / "contracts" / "expense-demo.json"))
        g = Q.plan(G, ["scripted_trust_response", "scripted_verify_no_resubmit"], "full", seed=5, validate_policy=GEN.validate_scripted)
        self.assertEqual(len(g["runs"]), 2 * 4 * 3 + 2 * 2)
        with self.assertRaises(ValueError): Q.plan(G, ["scripted_blind_retry"], "full", validate_policy=GEN.validate_scripted)   # positive control is not a candidate

class Upstream(BaseHTTPRequestHandler):
    commits = []
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
        Upstream.commits.append((body.get("k"), self.headers.get("Cookie"))); data = b'{"ok":true}'
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        data = b'{"page":true}'; self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

class TestFaultProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream); threading.Thread(target=up.serve_forever, daemon=True).start()
        cls.port = free_port(); cls.secret = "s3cret-control"
        cfg = {"upstream": f"http://127.0.0.1:{up.server_address[1]}", "listen_port": cls.port, "secret": cls.secret, "scope": {"in": "json_body", "field": "k"},
               "watch": [{"name": "do_thing", "method": "POST", "path_regex": "^/act$"}], "fabricated": {"status": 200, "body": {"fake": True}}}
        env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "AFD_PROXY_CONFIG": json.dumps(cfg)}
        cls.p = subprocess.Popen([sys.executable, "-I", str(ROOT / "afd" / "faults" / "proxy.py")], env=env)
        cls.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try: cls.op.open(f"http://127.0.0.1:{cls.port}/_afd/healthz", timeout=1)
            except urllib.error.HTTPError: break
            except OSError: time.sleep(0.1)
    @classmethod
    def tearDownClass(cls): cls.p.terminate(); cls.p.wait(5)
    def req(self, path, body=None, headers=None, method=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body is not None else None, method=method or ("POST" if body is not None else "GET"),
                                   headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self.op.open(r, timeout=20) as resp: return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"null")
    def arm(self, k, kind, delay=1): return self.req("/_afd/arm", {"scope": k, "kind": kind, "delay": delay}, {"X-AFD-Secret": self.secret})[0]
    def ev(self, k): return self.req(f"/_afd/evidence?scope={k}", headers={"X-AFD-Secret": self.secret})[1]
    def test_control_requires_secret(self):
        self.assertEqual(self.req("/_afd/evidence?scope=x")[0], 403); self.assertEqual(self.req("/_afd/arm", {"scope": "x", "kind": "lost_ack"})[0], 403)
    def test_primitives_and_allow_listed_evidence(self):
        Upstream.commits.clear()
        self.assertEqual(self.req("/act", {"k": "c1"}, {"Cookie": "sid=browser-cookie"})[0], 200)                     # clean
        self.assertEqual(self.arm("l1", "lost_ack"), 200)
        self.assertEqual(self.req("/act", {"k": "l1"})[0], 503); self.assertEqual(self.req("/act", {"k": "l1"})[0], 200)
        self.assertEqual(self.arm("f1", "fabricated"), 200)
        self.assertEqual(self.req("/act", {"k": "f1"}), (200, {"fake": True})); self.assertEqual(self.req("/act", {"k": "f1"})[0], 200)
        self.assertEqual(self.arm("d1", "delayed_commit", 1), 200); self.assertEqual(self.req("/act", {"k": "d1"})[0], 503)
        self.assertEqual(self.ev("d1")["pending_late_commit"], True); time.sleep(2)
        self.assertEqual([k for k, _ in Upstream.commits], ["c1", "l1", "l1", "d1"])            # fabricated never forwarded
        self.assertEqual(Upstream.commits[0][1], "sid=browser-cookie")                           # the app still receives the client's headers
        l1, f1, d1 = self.ev("l1"), self.ev("f1"), self.ev("d1")
        self.assertEqual([(r["status_to_client"], r["intervention"], r["outcome"], r["effect_index"]) for r in l1["requests"]], [(503, "lost_ack", "committed", 0), (200, "none", "committed", 1)])
        self.assertEqual([(r["intervention"], r["outcome"]) for r in f1["requests"]], [("fabricated", "fabricated_success")] * 2); self.assertEqual(f1["effects"], [])
        self.assertEqual((d1["requests"][0]["outcome"], d1["requests"][0]["effect_index"], d1["pending_late_commit"]), ("committed_late", 0, False))
        blob = json.dumps([l1, f1, d1, self.ev("c1")])
        self.assertNotIn("browser-cookie", blob)
        self.assertTrue(all(list(r) == ["seq", "route", "t_received", "t_responded", "status_to_client", "intervention", "outcome", "effect_index"] for e in (l1, f1, d1) for r in e["requests"]))
    def test_unwatched_and_unscoped_pass_through_without_evidence(self):
        self.assertEqual(self.req("/page")[0], 200); self.assertEqual(self.ev("")["requests"], [])

if __name__ == "__main__":
    unittest.main()
