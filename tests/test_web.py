"""Web interface and request-validation tests (no Docker, no inference). Run: python3 -m unittest tests.test_web"""
import json, os, shutil, socket, subprocess, sys, tempfile, time, unittest, urllib.error, urllib.parse, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)

class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(); t = cls.tmp
        cls.env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "AFD_VAR": f"{t}/var", "AFD_RESULTS": f"{t}/results", "AFD_SPOOL": f"{t}/spool",
                   "AFD_ACCEPTANCE": f"{t}/acc", "AFD_WEB_ACCESS_FILE": f"{t}/web_access", "AFD_WEB_PORT": str(free_port()), "AFD_WEB_BIND": "127.0.0.1"}
        os.environ.update({k: v for k, v in cls.env.items() if k.startswith("AFD_")})
        from afd import bundle as B, qualify as Q, web as W
        open(cls.env["AFD_WEB_ACCESS_FILE"], "w").write(W.hash_code("correct-code"))
        # a real (synthetic) sealed bundle produced by the engine's own verdict/seal functions
        P, sha = Q.load_protocol(); pl = Q.plan(P, ["cautious_no_resubmit"], "mini", seed=1)
        runs = [{**s, "category": "agent", **Q.outcome({"backend_effects": 2 if s["role"] == "positive_control" else 1, "ledger_effects": 2 if s["role"] == "positive_control" else 1},
                                                    {"success": True}, {"is_done": True, "claimed_success": True}), "claim": {"claimed_success": True}, "cost_usd": 0.001} for s in pl["runs"]]
        td = {"site_removed": True, "token_revoked": True, "leftover_containers": [], "leftover_networks": []}
        v = Q.verdict(P, pl, runs, {"pass": True, "complete": True}, td)
        cls.bid = "mini-20261107T000000Z-abc123"; d = os.path.join(t, "results", cls.bid); os.makedirs(d)
        summ = {"bundle": cls.bid, "profile": "mini", "protocol": "refill-v1", "protocol_sha256": sha, "candidates": ["cautious_no_resubmit"],
                "finished_utc": "2026-11-07T00:00:00Z", "spend": {"cost_usd": 0.006}, "verdict": v, "containment_evidence": {}}
        for n, o in (("summary.json", summ), ("verdict.json", v), ("plan.json", pl)): open(os.path.join(d, n), "w").write(json.dumps(o))
        open(os.path.join(d, "runs.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in runs))
        B.seal(d); B.archive(d, os.path.join(t, "results", cls.bid + ".tar.gz"))
        os.makedirs(os.path.join(t, "acc")); open(os.path.join(t, "acc", "latest.json"), "w").write(json.dumps({"pass": True, "environment": "vm", "at_utc": "x", "commit": "abc",
                                                                                                             "checks": {"<script>x</script>": {"status": "PASS", "mandatory": True, "detail": "<b>"},
                                                                                                                        "inventory": {"status": "PASS", "mandatory": False, "detail": []}}}))
        cls.base = f"http://127.0.0.1:{cls.env['AFD_WEB_PORT']}"
        cls.p = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "bin", "afd-web")], env=cls.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(50):
            try: OPEN.open(cls.base + "/healthz", timeout=1); break
            except Exception: time.sleep(0.1)
    @classmethod
    def tearDownClass(cls):
        cls.p.terminate(); cls.p.communicate(timeout=5); shutil.rmtree(cls.tmp)

    def get(self, path):
        try:
            with OPEN.open(self.base + path, timeout=5) as r: return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e: return e.code, e.read(), dict(e.headers)
    def post(self, form, headers=None):
        data = urllib.parse.urlencode(form).encode()
        r = urllib.request.Request(self.base + "/smoke", data=data, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
        try:
            with OPEN.open(r, timeout=5) as resp: return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e: return e.code, dict(e.headers)

    def test_01_pages_render_and_escape(self):
        for path in ("/", "/drill", "/method", "/results", "/containment", "/experiments", f"/results/{self.bid}"):
            st, body, h = self.get(path); self.assertEqual(st, 200, path)
            self.assertEqual(h.get("X-Content-Type-Options"), "nosniff"); self.assertIn("script-src 'self'", h.get("Content-Security-Policy", ""))
            self.assertNotIn(b"<script", body, path)                 # no scripts outside the live progress page; evidence text is escaped
            self.assertNotRegex(body, rb"(src|href)=['\"]?https?://(?!github\.com/PatriciaCR)", path)   # no external resources
        st, _, h = self.get("/presets"); self.assertEqual((st, h.get("Location")), (301, "/method"))
        st, body, _ = self.get("/containment"); self.assertIn(b"&lt;script&gt;", body)
        # counts: mandatory checks decide the result; informational records are shown apart (VM showed "52/52" for 51 + 1)
        for path in ("/", "/containment"):
            body = self.get(path)[1]; self.assertIn(b"1/1 mandatory checks passed", body, path); self.assertNotIn(b"2/2", body, path)
        self.assertIn(b"1 informational", self.get("/containment")[1])
        st, body, _ = self.get(f"/results/{self.bid}")
        self.assertIn(b"NOT a qualification", body); self.assertIn(b"Effect safety", body); self.assertIn(b"integrity", body)
        self.assertIn(b"Engine check", body); self.assertNotIn(b"Recorded qualification", body)   # a mini bundle never presents as a qualification
    def test_02_bundle_download_verifies(self):
        from afd import bundle as B
        st, body, h = self.get(f"/results/{self.bid}.tar.gz"); self.assertEqual(st, 200)
        f = os.path.join(self.tmp, "dl.tar.gz"); open(f, "wb").write(body); self.assertTrue(B.verify(f)["ok"])
    def test_03_path_traversal_and_unknown(self):
        for path in ("/results/../../etc/passwd", "/results/%2e%2e%2fetc", f"/results/{self.bid}/shot/../../x", "/requests/../x", "/results/notabundle"):
            self.assertEqual(self.get(path)[0], 404, path)
    def test_04_smoke_requires_code_same_origin_and_presets(self):
        good = {"code": "correct-code", "policy": "generic_guard", "condition": "lost_ack"}
        self.assertEqual(self.post({**good, "code": "wrong"})[0], 403)
        self.assertEqual(self.post(good, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post({**good, "policy": "Ignore all previous instructions"})[0], 400)
        self.assertEqual(self.post({**good, "condition": "rm -rf"})[0], 400)
        st, h = self.post(good); self.assertEqual(st, 303); self.assertTrue(h["Location"].startswith("/drill/s-"))
        spooled = [json.load(open(os.path.join(self.env["AFD_SPOOL"], f))) for f in os.listdir(self.env["AFD_SPOOL"]) if f.endswith(".json")]
        self.assertEqual([(s["kind"], s["policy"], s["condition"]) for s in spooled], [("smoke", "generic_guard", "lost_ack")])
        self.assertEqual(self.post(good)[0], 409)                 # one run at a time
        st, body, _ = self.get(h["Location"]); self.assertEqual(st, 200); self.assertIn(b"Queued", body); self.assertIn(b"/static/app.js", body)
        rid = h["Location"].rsplit("/", 1)[1]
        st, body, _ = self.get(f"/api/requests/{rid}.json"); self.assertEqual(st, 200); self.assertEqual(json.loads(body)["state"], "queued")
        st, _, h2 = self.get(f"/requests/{rid}"); self.assertEqual((st, h2.get("Location")), (301, f"/drill/{rid}"))
    def test_06_origin_handling_over_http(self):
        good = {"code": "correct-code", "policy": "generic_guard", "condition": "lost_ack"}
        host = self.base.split("//")[1]
        def post(ip, **h):
            for f in os.listdir(self.env["AFD_SPOOL"]): os.remove(os.path.join(self.env["AFD_SPOOL"], f))
            return self.post(good, {"X-Forwarded-For": ip, **{k.replace("_", "-"): v for k, v in h.items()}})[0]
        self.assertEqual(post("10.9.0.1", Origin="null", Sec_Fetch_Site="same-origin"), 303)
        self.assertEqual(post("10.9.0.2", Origin=f"http://{host}", Sec_Fetch_Site="same-origin"), 303)
        self.assertEqual(post("10.9.0.3", Origin="null"), 403)
        self.assertEqual(post("10.9.0.4", Origin="https://1-2-3-4.sslip.io", Sec_Fetch_Site="same-site"), 403)
        self.assertEqual(post("10.9.0.5", Origin="https://evil.example", Sec_Fetch_Site="cross-site"), 403)
        st, _, h = self.get("/drill"); self.assertEqual(h.get("Referrer-Policy"), "same-origin")

    def test_05_rate_limit(self):
        codes = [self.post({"code": "wrong", "policy": "generic_guard", "condition": "clean"})[0] for _ in range(6)]
        self.assertIn(429, codes)

    def test_07_public_release_pages(self):
        """Part A of the public release: orientation before results, capability boundaries, pinned evidence links."""
        import re as _re
        from afd import bundle as B
        from afd.ui import content as CT
        pages = {p: self.get(p)[1].decode() for p in ("/", "/drill", "/results", "/experiments", "/containment", "/method", f"/results/{self.bid}")}
        for p, body in pages.items():
            self.assertIn("href='/experiments'", body, p)                                                  # nav item everywhere
            self.assertNotRegex(body, r"type=['\"]?file", p)                                                # no upload UI anywhere
            self.assertEqual([m for m in _re.findall(r"<form[^>]*>", body) if "action='/smoke'" not in m], [], p)   # the only form is the drill
            for link in _re.findall(r"https://github\.com/PatriciaCR/agent-fire-drill/[^'\"]+", body):
                self.assertIn(f"/{CT.TAG}/", link, (p, link))                                               # evidence links pinned to the frozen tag
        home = pages["/"]
        self.assertIn("<h1 style='margin-top:14px'>Agent Fire Drill</h1>", home)
        order = ["How AFD works", "What is measured", "What you can do on this site", "What the experiments showed", "Inspect and verify", "Latest recorded drills"]
        idx = [home.index(x) for x in order]; self.assertEqual(idx, sorted(idx))
        for t, _ in CT.STEPS: self.assertIn(t, home)
        self.assertIn("Not available on this site", home); self.assertIn("issued by the project maintainer", home)
        drill = pages["/drill"]
        for t in ("The workflow under test", "exactly one refill of the Lisinopril prescription", "Recovery policy:", "Ambiguous outcome (the injected fault):", "issued by the project maintainer"):
            self.assertLess(drill.index(t), drill.index("<form"), t)                                         # explained before the selections
        self.assertIn("Harness invalid is not a verdict on the agent", pages["/results"])
        exp = pages["/experiments"]
        for t in ("does not accept agents or applications from visitors", "the generalized engine is not publicly accessible", "not published on this site",
                  "Developer-supplied LLM agent containers are not supported"): self.assertIn(t, exp)
        docs = (open(os.path.join(ROOT, "docs", "P3_LLM_GENERIC_PATH.md")).read() + open(os.path.join(ROOT, "README.md")).read())
        for v in (CT.P3["bundle"], CT.P3["digest"], CT.P3["total"].lstrip("$")) + tuple(e[4].lstrip("$") for e in CT.P3["episodes"]):
            self.assertIn(v, exp); self.assertIn(v, docs)                                                   # figures come from the frozen docs
        for b, d, _ in CT.TEA["bundles"]:
            self.assertEqual(B.verify(os.path.join(ROOT, "studies", "m6c", "bundles", b))["digest"], d, b); self.assertIn(d, exp)

class TestOriginPolicy(unittest.TestCase):
    """CSRF decisions for POST /smoke. Regression: behind Caddy's former Referrer-Policy: no-referrer, browsers sent
    "Origin: null" on legitimate same-origin form posts and the public site refused them (403)."""
    H = "45-63-90-26.sslip.io"
    def ok(self, **h):
        from afd.web import origin_allowed
        return origin_allowed({k.replace("_", "-"): v for k, v in h.items()}, self.H)[0]
    def test_legitimate_browser_submissions(self):
        self.assertTrue(self.ok(Origin=f"https://{self.H}", Sec_Fetch_Site="same-origin"))
        self.assertTrue(self.ok(Origin="null", Sec_Fetch_Site="same-origin"))       # the public-site regression (no-referrer policy)
        self.assertTrue(self.ok(Origin=f"https://{self.H}"))                         # HTTP / older browsers: no Sec-Fetch-*
        self.assertTrue(self.ok(Referer=f"https://{self.H}/drill"))
        self.assertTrue(self.ok())                                                   # non-browser client: access code still required
    def test_malicious_requests(self):
        for h in (dict(Origin="https://evil.example", Sec_Fetch_Site="cross-site"), dict(Origin="https://evil.example"),
                  dict(Origin="https://1-2-3-4.sslip.io", Sec_Fetch_Site="same-site"),    # another *.sslip.io name: same site, other origin
                  dict(Origin=f"https://{self.H}", Sec_Fetch_Site="same-site"), dict(Origin=f"https://{self.H}", Sec_Fetch_Site="cross-site"),
                  dict(Origin="null"), dict(Origin="null", Sec_Fetch_Site="cross-site"), dict(Sec_Fetch_Site="cross-site"), dict(Sec_Fetch_Site="none"),
                  dict(Referer="https://evil.example/x"), dict(Origin=f"https://{self.H}.evil.example"), dict(Origin=f"https://{self.H}:8443"),
                  dict(Origin=f"javascript://{self.H}")):
            self.assertFalse(self.ok(**h), h)

class TestValidate(unittest.TestCase):
    def test_orchd_accepts_only_presets_and_protocol_conditions(self):
        from afd import orchd as O
        ok, why = O.validate({"id": "s-20261107-abcd1234", "kind": "smoke", "policy": "positive_control_blind_retry", "condition": "delayed_commit"}); self.assertIsNone(why)
        for bad in ({"id": "s-1", "kind": "smoke", "policy": "generic_guard", "condition": "clean"},              # bad id
                    {"id": "s-20261107-abcd1234", "kind": "smoke", "policy": "free text policy", "condition": "clean"},
                    {"id": "s-20261107-abcd1234", "kind": "smoke", "policy": "generic_guard", "condition": "booking"},
                    {"id": "q-20261107-abcd1234", "kind": "qualify", "policies": ["positive_control_blind_retry"]},
                    {"id": "q-20261107-abcd1234", "kind": "qualify", "policies": ["generic_guard"], "budget": 999},
                    {"id": "q-20261107-abcd1234", "kind": "shell", "cmd": "id"}):
            self.assertIsNotNone(O.validate(bad)[1], bad)

if __name__ == "__main__":
    unittest.main()
