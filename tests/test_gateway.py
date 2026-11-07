"""Gateway acceptance tests against a fake upstream (no real inference, no cost). Run: python3 -I -m unittest tests.test_gateway"""
import hashlib, json, os, socket, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_KEY = "sk-test-" + "k" * 30
SEEN = []          # (headers, body) of every request the fake upstream received
MODE = {"m": "ok"}

class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((dict(self.headers), body))
        m = MODE["m"]
        if m == "error": self.send_response(500); self.end_headers(); self.wfile.write(b'{"error":"boom"}'); return
        resp = {"model": body["model"], "choices": [{"message": {"content": "hi"}, "finish_reason": "length" if m == "truncated" else "stop"}]}
        if m != "nousage": resp["usage"] = {"prompt_tokens": 1000, "completion_tokens": 8192 if m == "truncated" else 50}
        data = json.dumps(resp).encode(); self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class TestGateway(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(); cls.tokens = os.path.join(cls.tmp, "tokens"); cls.ledger = os.path.join(cls.tmp, "ledger")
        os.makedirs(cls.tokens); os.makedirs(cls.ledger)
        kf = os.path.join(cls.tmp, "key"); open(kf, "w").write(f"VULTR_INFERENCE_API_KEY={FAKE_KEY}\n"); os.chmod(kf, 0o400)
        up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream); cls.up_port = up.server_address[1]
        threading.Thread(target=up.serve_forever, daemon=True).start()
        cls.port = free_port()
        env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "AFD_UPSTREAM_BASE": f"http://127.0.0.1:{cls.up_port}/v1",
               "AFD_KEY_FILE": kf, "AFD_TOKEN_DIR": cls.tokens, "AFD_LEDGER_DIR": cls.ledger, "AFD_PORT": str(cls.port)}
        cls.proc = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "images", "gateway", "gateway.py")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(50):
            try: urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/healthz", timeout=1); break
            except Exception: time.sleep(0.1)
    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); out, err = cls.proc.communicate(timeout=5)
        assert FAKE_KEY.encode() not in out + err, "key appeared in gateway stdout/stderr"

    def token(self, job, budget=1.0, models=("glm-5.3-flash",), max_tokens=8192):
        tok = os.urandom(16).hex()
        json.dump({"job_id": job, "budget_usd": budget, "models": list(models), "max_tokens": max_tokens},
                  open(os.path.join(self.tokens, hashlib.sha256(tok.encode()).hexdigest() + ".json"), "w"))
        return tok

    def call(self, tok, body=None, headers=None):
        body = body or {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100}
        h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {}), **(headers or {})}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data=json.dumps(body).encode(), method="POST", headers=h)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(r, timeout=10) as resp: return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

    def ledger_rows(self, job):
        p = os.path.join(self.ledger, f"{job}.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def test_01_auth_and_revocation(self):
        self.assertEqual(self.call(None)[0], 401); self.assertEqual(self.call("nope")[0], 401)
        tok = self.token("j-auth"); self.assertEqual(self.call(tok)[0], 200)
        for f in os.listdir(self.tokens): os.remove(os.path.join(self.tokens, f))  # revoke all
        self.assertEqual(self.call(tok)[0], 401)

    def test_02_model_stream_size(self):
        tok = self.token("j-pol")
        self.assertEqual(self.call(tok, {"model": "glm-5.3", "messages": []})[0], 403)
        self.assertEqual(self.call(tok, {"model": "glm-5.3-flash", "messages": [], "stream": True})[0], 400)

    def test_03_key_only_upstream_client_headers_dropped_and_clamped(self):
        SEEN.clear(); tok = self.token("j-hdr", max_tokens=8192)
        st, _ = self.call(tok, {"model": "glm-5.3-flash", "messages": [], "max_completion_tokens": 99999}, headers={"X-Evil": "1", "Cookie": "c"})
        self.assertEqual(st, 200)
        h, body = SEEN[-1]
        self.assertEqual(h.get("Authorization"), f"Bearer {FAKE_KEY}")
        self.assertNotIn("X-Evil", h); self.assertNotIn("Cookie", h)
        self.assertEqual(body["max_completion_tokens"], 8192)

    def test_04_usage_metering_including_truncation(self):
        tok = self.token("j-use")
        MODE["m"] = "ok"; self.call(tok)
        MODE["m"] = "truncated"; self.call(tok)
        MODE["m"] = "nousage"; self.call(tok)
        MODE["m"] = "error"; st, _ = self.call(tok); MODE["m"] = "ok"
        self.assertEqual(st, 500)
        rows = self.ledger_rows("j-use")
        self.assertEqual([r["status"] for r in rows], [200, 200, 200, 500])
        self.assertEqual(rows[1]["finish_reason"], "length"); self.assertEqual(rows[1]["completion_tokens"], 8192)
        self.assertFalse(rows[1]["estimated_upper_bound"])
        self.assertTrue(rows[2]["estimated_upper_bound"])            # success without usage -> worst case
        self.assertAlmostEqual(rows[0]["cost_usd"], 1000 * 0.10e-6 + 50 * 0.35e-6, places=10)

    def test_05_budget_enforced_before_upstream(self):
        SEEN.clear(); tok = self.token("j-bud", budget=0.0005)
        st, body = self.call(tok, {"model": "glm-5.3-flash", "messages": [], "max_tokens": 8192})  # worst case > budget
        self.assertEqual(st, 402); self.assertEqual(SEEN, [])
        rows = self.ledger_rows("j-bud")   # the refusal is audited at zero cost so budget stops are distinguishable
        self.assertEqual([(r["status"], r["cost_usd"], r.get("refused")) for r in rows], [(402, 0.0, "budget")])

    def test_06_unreachable_upstream_charged_worst_case(self):
        # point a second gateway at a closed port
        port, dead = free_port(), free_port()
        kf = os.path.join(self.tmp, "key")
        env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "AFD_UPSTREAM_BASE": f"http://127.0.0.1:{dead}/v1",
               "AFD_KEY_FILE": kf, "AFD_TOKEN_DIR": self.tokens, "AFD_LEDGER_DIR": self.ledger, "AFD_PORT": str(port), "AFD_UPSTREAM_TIMEOUT": "2"}
        p = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "images", "gateway", "gateway.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.6); tok = self.token("j-dead")
            r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", method="POST", headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                                       data=json.dumps({"model": "glm-5.3-flash", "messages": [], "max_tokens": 10}).encode())
            try: urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=10); code = 200
            except urllib.error.HTTPError as e: code = e.code
            self.assertEqual(code, 502)
            row = self.ledger_rows("j-dead")[-1]; self.assertTrue(row["estimated_upper_bound"]); self.assertGreater(row["cost_usd"], 0)
        finally:
            p.terminate(); p.communicate(timeout=5)

    def test_07_key_from_stdin_and_credentials_dir_bind_and_ledger_mode(self):
        """Host-service modes: key read once from an inherited pipe ("-") or $CREDENTIALS_DIRECTORY; AFD_BIND honoured;
        ledger files created 0640 (not world-readable)."""
        for mode in ("stdin", "credentials"):
            port = free_port(); SEEN.clear()
            env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower() and k != "AFD_KEY_FILE"}, "AFD_UPSTREAM_BASE": f"http://127.0.0.1:{self.up_port}/v1",
                   "AFD_TOKEN_DIR": self.tokens, "AFD_LEDGER_DIR": self.ledger, "AFD_PORT": str(port), "AFD_BIND": "127.0.0.1"}
            if mode == "stdin": env["AFD_KEY_FILE"] = "-"
            else:
                cd = os.path.join(self.tmp, "creds"); os.makedirs(cd, exist_ok=True); kf = os.path.join(cd, "vultr_key")
                if not os.path.exists(kf): open(kf, "w").write(FAKE_KEY + "\n")
                env["CREDENTIALS_DIRECTORY"] = cd
            p = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "images", "gateway", "gateway.py")], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 preexec_fn=lambda: os.umask(0o077))   # a restrictive umask must not make usage unreadable
            if mode == "stdin": p.stdin.write(f"VULTR_INFERENCE_API_KEY={FAKE_KEY}\n".encode()); p.stdin.close()
            try:
                for _ in range(50):
                    try: urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{port}/healthz", timeout=1); break
                    except Exception: time.sleep(0.1)
                tok = self.token(f"j-{mode}")
                r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", method="POST", headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                                           data=json.dumps({"model": "glm-5.3-flash", "messages": [], "max_tokens": 10}).encode())
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r, timeout=10) as resp: self.assertEqual(resp.status, 200)
                self.assertEqual(SEEN[-1][0].get("Authorization"), f"Bearer {FAKE_KEY}")
                mode_bits = os.stat(os.path.join(self.ledger, f"j-{mode}.jsonl")).st_mode & 0o777
                self.assertEqual(mode_bits, 0o640, oct(mode_bits))
            finally:
                p.terminate(); out, err = p.communicate(timeout=5); self.assertNotIn(FAKE_KEY.encode(), out + err)

    def test_99_key_never_logged_or_written(self):
        blobs = []
        for f in os.listdir(self.ledger):
            with open(os.path.join(self.ledger, f)) as fh: blobs.append(fh.read())
        self.assertFalse(any(FAKE_KEY in b for b in blobs))
        # process output is checked after shutdown in tearDownClass via a separate assertion below

if __name__ == "__main__":
    unittest.main()
