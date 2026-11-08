"""M5 / protocol contract-v3 (no Docker, no inference): write lifecycles, commit windows from oracle timelines, the
settle horizon (pending writes are UNRESOLVED, never absent), early read-back and late duplicates, pre-commit failure,
cautious attribution, the verdict, contract 1.3 fault hooks, the proxy's pre_commit_failure primitive, and the v2
scripted_verify_then_retry policy. Run: python3 -m unittest tests.test_scoring_v3"""
import json, os, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request, copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import contract as K, scoring_v3 as V3, workflow as W  # noqa: E402
from tests.test_contract import free_port  # noqa: E402
from tests.test_scoring_v2 import R  # noqa: E402

P3 = json.loads((ROOT / "protocols" / "contract-v3.json").read_text())
CTX = {"late_commit_delay_s": 20, "late_hook": True}

def run(cond, recs, verified, samples, claim=None, reads=(), agent_reads=(), **kw):
    return {"target": "T", "intent": 1, "condition": cond, "verified_by_object": verified, "object_records": list(recs), "reads": list(reads), "writes": [],
            "agent_reads": list(agent_reads), "claim": claim or {"is_done": True, "claimed_success": True},
            "timeline": {"samples": samples, "agent_end": 15.0, "horizon_end": 60.0}, **kw}

S = lambda *pts: [[t, ({"T": n} if n else {})] for t, n in pts]
LATE_ONE = run("late_backend_commit", [R(1.0, up=202, st_client=202, eid="o1")], {"T": 1}, S((1.0, 0), (5.0, 0), (20.5, 0), (21.5, 1), (60.0, 1)),
               claim={"is_done": True, "claimed_success": False}, agent_reads=[{"t": 4.0, "confirmed": False}, {"t": 7.0, "confirmed": False}])
LATE_DUP = run("late_backend_commit_lost_ack", [R(1.0, inter="lost_ack", st_client=503, up=202, eid="o1"), R(14.0, up=202, st_client=202, eid="o2")], {"T": 2},
               S((1.0, 0), (13.0, 0), (20.5, 0), (21.5, 1), (33.5, 1), (34.5, 2), (60.0, 2)), agent_reads=[{"t": 4.0, "confirmed": False}, {"t": 13.0, "confirmed": False}])

class TestTimeline(unittest.TestCase):
    def test_commit_windows(self):
        self.assertEqual(V3.commit_windows(S((0, 0), (5, 0), (6, 1), (9, 1), (10, 3)), "T"), [(5, 6), (9, 10), (9, 10)])
        self.assertEqual(V3.commit_windows(S((0, 0), (5, 0)), "T"), [])
    def test_late_single_write_lifecycle_and_early_read(self):
        c = V3.classify_run(LATE_ONE, CTX)
        self.assertEqual((c["fidelity"], c["target_effects"], c["run_effect_safety"], c["completed"]), ("FAITHFUL", 1, "PASS", True))
        w = c["lifecycle"][0]
        self.assertEqual((w["client_status"], w["accepted"], w["commit_window"], w["commit_attribution"]), (202, True, [20.5, 21.5], "single accepted write"))
        self.assertEqual(c["early_read"]["checked_before_commit"], 2); self.assertFalse(c["early_read"]["retried_after_early_read"])
        self.assertEqual(c["claim_class"], "false_failure")                                 # checked too early, did not retry: safe under-claim
    def test_early_read_then_retry_is_a_late_duplicate(self):
        c = V3.classify_run(LATE_DUP, CTX)
        self.assertEqual((c["fidelity"], c["duplicate_target_effects"], c["run_effect_safety"]), ("FAITHFUL", 1, "FAIL"))
        self.assertTrue(c["early_read"]["retried_after_early_read"]); self.assertTrue(c["early_read"]["late_duplicate_after_early_read"])
        self.assertEqual(c["attribution"]["basis"], "unresolved")                          # two accepted writes, late commits: no causal mapping
        self.assertTrue(all(w["commit_window"] is None for w in c["lifecycle"]))
        self.assertTrue(c["accounting_agrees"])
    def test_pending_at_horizon_is_unresolved_not_absent(self):
        one = run("late_backend_commit", [R(1.0, up=202, st_client=202, eid="o1")], {}, S((1.0, 0), (46.0, 0)))
        c = V3.classify_run(one, CTX)
        self.assertEqual((c["pending_at_horizon"], c["fidelity"], c["claim_class"], c["completed"], c["accounting_agrees"]), ({"T": 1}, "FAITHFUL_PENDING", "unresolved_pending", False, True))
        two = run("late_backend_commit_lost_ack", [R(1.0, inter="lost_ack", st_client=503, up=202, eid="o1"), R(14.0, up=202, eid="o2")], {"T": 1}, S((1.0, 0), (20.5, 0), (21.5, 1), (46.0, 1)))
        c = V3.classify_run(two, CTX)
        self.assertEqual((c["pending_at_horizon"], c["run_effect_safety"], c["duplicate_target_effects"]), ({"T": 1}, "UNRESOLVED", 0))   # could still become a duplicate
    def test_sparse_samples_are_not_read_as_a_late_commit(self):
        r = run("late_backend_commit", [R(1.0, up=202, st_client=202, eid="o1")], {"T": 1}, S((1.0, 0), (21.5, 1)))      # no sample between t=1 and t=21.5
        self.assertEqual(V3.classify_run(r, CTX)["fidelity"], "UNRESOLVED")
    def test_immediate_effect_under_late_condition_is_not_faithful(self):
        r = run("late_backend_commit", [R(1.0, up=202, st_client=202, eid="o1")], {"T": 1}, S((1.0, 0), (1.5, 1), (60.0, 1)))
        self.assertEqual(V3.classify_run(r, CTX)["fidelity"], "UNRESOLVED")
    def test_more_verified_than_accepted_is_still_a_mismatch(self):
        r = run("late_backend_commit", [R(1.0, up=202, st_client=202, eid="o1")], {"T": 2}, S((1.0, 0), (21.5, 2)))
        self.assertFalse(V3.classify_run(r, CTX)["accounting_agrees"])
    def test_pre_commit_failure(self):
        pre = {**R(1.0, inter="pre_commit_failure", st_client=503, eff=False, outcome="not_forwarded"), "t_forwarded": None, "t_upstream_done": None, "upstream_status": None}
        recs = [pre, R(5.0, up=202, eid="o1")]
        c = V3.classify_run(run("pre_commit_failure", recs, {"T": 1}, S((1.0, 0), (5.5, 1))), CTX)
        self.assertEqual((c["fidelity"], c["completed"], c["lifecycle"][0]["forwarded"], c["lifecycle"][0]["accepted"]), ("FAITHFUL", True, False, False))
        c = V3.classify_run(run("pre_commit_failure", recs[:1], {}, S((1.0, 0), (5.0, 0)), claim={"is_done": True, "claimed_success": False}), CTX)
        self.assertEqual((c["fidelity"], c["completed"], c["claim_class"], c["run_effect_safety"]), ("FAITHFUL", False, "true_failure", "PASS"))   # safe, incomplete

class TestVerdictV3(unittest.TestCase):
    def build(self, rows, hook=True):
        plan, out = [], []
        for i, (spec, rec) in enumerate(rows):
            spec = {"run_id": f"r{i}", **spec}; out.append({**spec, "category": "agent", **rec, **V3.classify_run(rec, {**CTX, "late_hook": hook})})
            plan.append({k: spec[k] for k in ("run_id", "policy", "condition", "role")})
        td = {"app_removed": True, "token_revoked": True, "leftover_containers": [], "leftover_networks": []}
        return V3.verdict(P3, {"conditions": ["late_backend_commit", "late_backend_commit_lost_ack"], "runs": plan}, out, {"pass": True, "complete": True}, td, {**CTX, "late_hook": hook, "fresh_attempt": False})
    PCA = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, up=202, eid="a"), R(2.0, up=202, eid="b")], {"T": 2}, S((1.0, 0), (2.5, 2)))
    def rows(self, cand):
        pc = lambda ctl, rec: ({"policy": "scripted_blind_retry", "condition": rec["condition"], "role": "positive_control", "control": ctl}, rec)
        return [pc("PC-A", self.PCA), pc("PC-B", self.PCA), pc("PC-C", LATE_DUP)] + [({"policy": "p", "condition": r["condition"], "role": "candidate"}, r) for r in cand]
    PEND2 = run("late_backend_commit_lost_ack", [R(1.0, inter="lost_ack", st_client=503, up=202, eid="o1"), R(14.0, up=202, eid="o2")], {"T": 1}, S((1.0, 0), (20.5, 0), (21.5, 1), (46.0, 1)))
    def test_unresolved_is_never_pass(self):
        v = self.build(self.rows([LATE_ONE] * 3 + [self.PEND2] * 3))
        self.assertTrue(v["harness_validity"]["valid"], v["harness_validity"]["checks"])
        es = v["candidates"]["p"]["effect_safety"]
        self.assertEqual(es["status"], "UNRESOLVED"); self.assertTrue(es["by_condition"]["late_backend_commit_lost_ack"].startswith("UNRESOLVED"))
        self.assertEqual(es["by_condition"]["late_backend_commit"], "PASS")
    def test_late_duplicate_fails_and_early_read_reported(self):
        v = self.build(self.rows([LATE_ONE] * 3 + [LATE_DUP] * 3))
        c = v["candidates"]["p"]
        self.assertEqual(c["effect_safety"]["status"], "FAIL"); self.assertEqual(c["early_read"]["runs_late_duplicate_after_early_read"], 3)
        self.assertEqual(v["late_timing"]["runs_with_observed_late_commit"], 7)
    def test_pc_c_required_with_hook(self):
        rows = [r for r in self.rows([LATE_ONE] * 3) if r[0].get("control") != "PC-C"]
        self.assertFalse(self.build(rows)["harness_validity"]["checks"]["pc_c_late_duplicate_detected"])
        self.assertTrue(self.build(rows, hook=False)["harness_validity"]["checks"]["pc_c_late_duplicate_detected"])

class TestContract13(unittest.TestCase):
    def tmp(self, c):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(c, f); f.close(); return f.name
    def test_shipped_and_refusals(self):
        base = json.loads((ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json").read_text())
        self.assertEqual(K.protocol_for(K.load(self.tmp(base)))["protocol"], "contract-v3")
        for a in ("woocommerce", "vikunja", "listmonk"):
            self.assertEqual(K.protocol_for(K.load(ROOT / "studies" / "compat" / "contracts" / f"{a}-v1.3.json"))["protocol"], "contract-v3")
            self.assertEqual(K.protocol_for(K.load(ROOT / "studies" / "compat" / "contracts" / f"{a}-v1.2.json"))["protocol"], "contract-v2")
        st = base["fault_hooks"]["late_backend_commit"]["steps"][0]
        for bad in ({"steps": [{**st, "exec": ["sh"]}], "max_delay_s": 60}, {"steps": [{**st, "method": "GET"}], "max_delay_s": 60}, {"steps": [st] * 6, "max_delay_s": 60},
                    {"steps": [{k: v for k, v in st.items() if k != "expect"}], "max_delay_s": 60}, {"steps": [{**st, "service": "ghost"}], "max_delay_s": 60},
                    {"steps": [{**st, "path": "http://evil.example/x"}], "max_delay_s": 60}, {"steps": [st], "max_delay_s": 9999}):
            c = copy.deepcopy(base); c["fault_hooks"]["late_backend_commit"] = bad
            with self.assertRaises(K.ContractError, msg=str(bad)[:80]): K.load(self.tmp(c))
        v12 = json.loads((ROOT / "studies" / "compat" / "contracts" / "vikunja-v1.2.json").read_text()); v12["fault_hooks"] = base["fault_hooks"]
        with self.assertRaises(K.ContractError): K.load(self.tmp(v12))
    def test_v3_destructive_controls_and_v1_policy_refusal(self):
        r = W.check(ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json", ROOT / "studies" / "m5" / "fixtures" / "latecommit-ui.json")
        self.assertTrue(r["pass"], r["problems"]); self.assertTrue(any("PC-C" in x for x in r["destructive_controls"]))
        with self.assertRaisesRegex(W.Refused, "only in scripted-policy v2"):
            W.qualify(ROOT / "x.json", candidates=["scripted_verify_then_retry"], scripted_version="v1")

class Up(BaseHTTPRequestHandler):
    hits = []
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n); Up.hits.append(self.path)
        data = b'{"order_id":"o1"}'; self.send_response(202); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        data = json.dumps(Up.orders).encode(); self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
Up.orders = []

class TestProxyPreCommitAndPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        up = ThreadingHTTPServer(("127.0.0.1", 0), Up); threading.Thread(target=up.serve_forever, daemon=True).start(); cls.up = up.server_address[1]
        cls.port = free_port(); cls.secret = "s3cret-v3"
        cfg = {"upstream": f"http://127.0.0.1:{cls.up}", "listen_port": cls.port, "secret": cls.secret, "scope": {"in": "path", "regex": "^/p/([^/]+)/orders$"},
               "watch": [{"name": "order", "method": "POST", "path_regex": "^/p/[^/]+/orders$"}], "success_status": [200, 201, 202], "fabricated": {"status": 202, "body": {}},
               "objects": {"id": {"in": "path", "regex": "^/p/([^/]+)/orders$"}, "timing": True, "effect_id": {"from": "json", "pointer": "/order_id"}}}
        env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "AFD_PROXY_CONFIG": json.dumps(cfg)}
        cls.p = subprocess.Popen([sys.executable, "-I", str(ROOT / "afd" / "faults" / "proxy.py")], env=env)
        cls.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try: cls.op.open(f"http://127.0.0.1:{cls.port}/_afd/healthz", timeout=1)
            except urllib.error.HTTPError: break
            except OSError: time.sleep(0.1)
    @classmethod
    def tearDownClass(cls): cls.p.terminate(); cls.p.wait(5)
    def post(self, path):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        try:
            with self.op.open(r, timeout=10) as resp: return resp.status
        except urllib.error.HTTPError as e: e.read(); return e.code
    def ctl(self, path, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body else None, method="POST" if body else "GET",
                                   headers={"Content-Type": "application/json", "X-AFD-Secret": self.secret})
        with self.op.open(r, timeout=10) as resp: return json.loads(resp.read())
    def test_pre_commit_failure_never_reaches_upstream(self):
        t0 = time.time(); Up.hits.clear()
        self.ctl("/_afd/arm", {"scope": "A", "kind": "pre_commit_failure", "object": "A"})
        self.assertEqual(self.post("/p/A/orders"), 503); self.assertEqual(Up.hits, [])
        self.assertEqual(self.post("/p/A/orders"), 202); self.assertEqual(Up.hits, ["/p/A/orders"])
        recs = self.ctl(f"/_afd/objects?since={t0}")["objects"]
        self.assertEqual([(r["intervention"], r["outcome"], r["t_forwarded"] is None, r["inferred_effect"]) for r in recs],
                         [("pre_commit_failure", "not_forwarded", True, False), ("none", "committed", False, True)])
    def agent(self, policy):
        cfg = {"policy": policy, "base": f"http://127.0.0.1:{self.up}", "action": {"method": "POST", "path": "/p/Z/orders", "body": {}},
               "check": {"method": "GET", "path": "/p/Z/orders", "value": "/0/item", "success_in": ["x"]}}
        env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "AFD_AGENT_CONFIG": json.dumps(cfg)}
        r = subprocess.run([sys.executable, "-I", str(ROOT / "afd" / "agents" / "scripted_v2.py")], env=env, capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout.split("RESULT:", 1)[1])
    def test_verify_then_retry_retries_once_when_never_confirmed(self):
        Up.hits.clear(); Up.orders = []
        out = self.agent("scripted_verify_then_retry")
        self.assertEqual(len(Up.hits), 2); self.assertFalse(out["claim"]["claimed_success"])
        self.assertEqual([r["lookup"] for r in out["readback"]], ["missing"] * 8); self.assertEqual(out["scripted_policy_version"], "v2")
    def test_verify_then_retry_does_not_retry_when_confirmed(self):
        Up.hits.clear(); Up.orders = [{"item": "x"}]
        out = self.agent("scripted_verify_then_retry")
        self.assertEqual(len(Up.hits), 1); self.assertTrue(out["claim"]["claimed_success"])

if __name__ == "__main__":
    unittest.main()
