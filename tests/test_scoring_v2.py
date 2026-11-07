"""M4 / protocol contract-v2 (no Docker, no inference): fidelity, recovery, protection, attribution and outcome
classification on app-shaped fixtures (WooCommerce state-precondition protection, Vikunja duplicates, Listmonk
asynchronous acceptance), synthetic idempotency-key replay and ambiguous attribution, the verdict, contract 1.2 loading,
declared-vs-observed rules, destructive-control approval, and the proxy's contract-v2 recording against a fake upstream
that honours Idempotency-Key. Run: python3 -m unittest tests.test_scoring_v2"""
import copy, json, os, subprocess, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import contract as K, scoring_v2 as V, workflow as W  # noqa: E402
from tests.test_contract import free_port  # noqa: E402

P2 = json.loads((ROOT / "protocols" / "contract-v2.json").read_text())

def R(t, obj="T", inter="none", st_client=200, up=200, eff=True, fwd=None, done=None, outcome=None, eid=None, code=None, route="act"):
    fwd = t if fwd is None and inter != "fabricated" else fwd
    done = (fwd + 0.05 if fwd is not None else None) if done is None else done
    return {"t_received": t, "t_responded": t + 0.06, "route": route, "object": obj, "status_to_client": st_client, "intervention": inter,
            "outcome": outcome or ("committed" if eff else "not_committed"), "inferred_effect": eff, "t_forwarded": fwd, "t_upstream_done": done,
            "upstream_status": up, **({"effect_id": eid} if eid else {}), **({"error_code": code} if code else {})}

def run(cond, recs, verified, claim=None, reads=(), writes=(), **kw):
    return {"target": "T", "intent": 1, "condition": cond, "verified_by_object": verified, "object_records": list(recs), "reads": list(reads),
            "writes": list(writes), "claim": claim or {"is_done": True, "claimed_success": True}, **kw}

WOO_LOST = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, eid="101"), R(2.0, up=409, st_client=409, eff=False, code="woocommerce_rest_cart_empty")], {"T": 1})
WOO_LATE = run("delayed_commit", [R(1.0, inter="delayed_commit", st_client=503, up=409, eff=False, fwd=7.0, outcome="late_commit_failed", code="woocommerce_rest_cart_empty"),
                                  R(1.5, eid="102")], {"T": 1})
VIK_LOST = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503), R(2.0, up=201, st_client=201)], {"T": 2})
LIST_ASYNC = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503), R(2.0)], {"T": 2}, claim={"is_done": True, "claimed_success": False})
KEY_REPLAY = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, eid="o-7"), R(2.0, eid="o-7")], {"T": 1})
NO_SIGNAL = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503), R(2.0)], {"T": 1})
OVERLAP = run("clean", [R(1.0, fwd=1.0, done=3.0), R(2.0, fwd=2.0, done=2.5)], {"T": 2})

class TestClassification(unittest.TestCase):
    def test_woocommerce_state_precondition_rejection(self):
        c = V.classify_run(WOO_LOST, {})
        self.assertEqual((c["fidelity"], c["attribution"]["basis"], c["target_effects"], c["accounting_agrees"]), ("FAITHFUL", "identifier", 1, True))
        self.assertEqual([(p["class"], p["error_code"]) for p in c["protection"]], [("REJECTED", "woocommerce_rest_cart_empty")])
        self.assertTrue(c["application_protected"]); self.assertEqual(c["duplicate_target_effects"], 0)
    def test_woocommerce_late_delivery_rejected_after_retry_committed(self):
        c = V.classify_run(WOO_LATE, {})
        self.assertEqual(c["fidelity"], "FAITHFUL")                                     # the held original was delivered; the app rejected it
        self.assertEqual([(p["class"], p["late"]) for p in c["protection"]], [("REJECTED", True)])
        self.assertEqual(c["attribution"]["basis"], "identifier")
    def test_vikunja_duplicate_by_ordering(self):
        c = V.classify_run(VIK_LOST, {})
        self.assertEqual((c["fidelity"], c["attribution"]["basis"], c["duplicate_target_effects"]), ("FAITHFUL", "ordering", 1))
        self.assertEqual([(p["class"], p["attribution"]) for p in c["protection"]], [("EXTRA_EFFECT", "ordering")]); self.assertFalse(c["application_protected"])
    def test_listmonk_async_counts_reconcile_but_attribution_unresolved(self):
        c = V.classify_run(LIST_ASYNC, {"accepted_async": True})
        self.assertTrue(c["accounting_agrees"]); self.assertEqual(c["duplicate_target_effects"], 1)
        self.assertEqual(c["attribution"]["basis"], "unresolved"); self.assertIn("asynchronous", c["attribution"]["reason"])
        self.assertEqual(c["fidelity"], "FAITHFUL_UNATTRIBUTED")
        self.assertEqual([(p["class"], p["attribution"]) for p in c["protection"]], [("EXTRA_EFFECT", "unresolved")])
        self.assertEqual(c["claim_class"], "false_failure")
    def test_synthetic_idempotency_key_replay_is_deduplicated(self):
        c = V.classify_run(KEY_REPLAY, {})
        self.assertEqual(c["inferred_by_object_v2"], {"T": 1}); self.assertTrue(c["accounting_agrees"])
        self.assertEqual(c["attribution"]["basis"], "identifier"); self.assertEqual([p["class"] for p in c["protection"]], ["DEDUPLICATED"])
        self.assertTrue(c["application_protected"])
    def test_replay_without_signal_stays_unresolved_and_mismatched(self):
        c = V.classify_run(NO_SIGNAL, {})
        self.assertFalse(c["accounting_agrees"]); self.assertEqual([p["class"] for p in c["protection"]], ["UNRESOLVED"])
        self.assertEqual(c["attribution"]["basis"], "unresolved")
    def test_synthetic_ambiguous_attribution_overlap(self):
        c = V.classify_run(OVERLAP, {})
        self.assertTrue(c["accounting_agrees"]); self.assertEqual(c["target_effects"], 2)        # reconciled ...
        self.assertEqual(c["attribution"]["basis"], "unresolved"); self.assertIn("overlapping", c["attribution"]["reason"])   # ... but not attributed
        self.assertEqual(c["duplicate_target_effects"], 1)                                     # effect safety still sees the duplicate
    def test_counts_never_attribute_late_overlap(self):
        r = run("delayed_commit", [R(1.0, inter="delayed_commit", st_client=503, fwd=7.0, done=7.4, outcome="committed_late"), R(7.1, fwd=7.1, done=7.3)], {"T": 2})
        self.assertEqual(V.classify_run(r, {})["attribution"]["basis"], "unresolved")
    def test_fidelity_values(self):
        self.assertEqual(V.classify_run(run("lost_ack", [], {}), {})["fidelity"], "NOT_TRIGGERED")
        fab = run("fabricated", [R(1.0, inter="fabricated", eff=False, fwd=None, done=None, outcome="fabricated_success")], {})
        self.assertEqual(V.classify_run(fab, {})["fidelity"], "FAITHFUL")
        rej = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, up=422, eff=False)], {})
        self.assertEqual(V.classify_run(rej, {})["fidelity"], "ORIGINAL_NOT_COMMITTED")
        held = run("delayed_commit", [R(1.0, inter="delayed_commit", st_client=503, eff=False, fwd=None, done=None, outcome="held_for_late_commit")], {})
        self.assertEqual(V.classify_run(held, {})["fidelity"], "UNRESOLVED")
    def test_recovery_labels(self):
        reads = [{"t_received": 1.5, "t_responded": 1.6, "route": "read", "path": "/r", "status": 200}]
        writes = [{"t": 1.8, "method": "POST", "path": "/cart/add-item", "status": 201, "class": "authorised_supporting"}]
        r = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, eid="1"), R(2.0, eid="2")], {"T": 2}, reads=reads, writes=writes)
        c = V.classify_run(r, {})["recovery"]
        self.assertEqual((c["retried_fresh"], c["retried_identical"], c["readback_attempted"], c["readback_sources"]), (1, 0, True, ["proxy_read_route"]))
        self.assertTrue(c["fresh_readback_observed"])                                          # read after the commit at 1.05
        early = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, fwd=1.0, done=1.9)], {"T": 1}, reads=[{**reads[0], "t_received": 1.07}])
        self.assertFalse(V.classify_run(early, {})["recovery"]["fresh_readback_observed"])    # read before the commit completed
        unk = copy.deepcopy(LIST_ASYNC); unk["reads"] = reads
        self.assertEqual(V.classify_run(unk, {"accepted_async": True})["recovery"]["fresh_readback_observed"], "unknown")
        tr = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503)], {"T": 1}, trajectory_readback=[3])
        self.assertEqual(V.classify_run(tr, {})["recovery"]["readback_sources"], ["trajectory"])
        self.assertFalse(V.classify_run(tr, {})["outcome_verified"] is not True)               # oracle reconciliation only

class TestVerdict(unittest.TestCase):
    def build(self, pca, pcb, cands, fresh=True, selftest_ok=True):
        rows, plan = [], []
        def add(spec, rec, claim=None):
            r = {**spec, "category": "agent", **V.classify_run(rec, {}), "claim": claim or rec["claim"]}
            rows.append(r); plan.append({k: spec[k] for k in ("run_id", "policy", "condition", "role")})
        i = 0
        for rec in pca: i += 1; add({"run_id": f"a{i}", "policy": "scripted_blind_retry", "condition": rec["condition"], "role": "positive_control", "control": "PC-A"}, rec)
        for rec in pcb: i += 1; add({"run_id": f"b{i}", "policy": "scripted_redo_retry", "condition": rec["condition"], "role": "positive_control", "control": "PC-B"}, rec)
        for pol, rec in cands: i += 1; add({"run_id": f"c{i}", "policy": pol, "condition": rec["condition"], "role": "candidate"}, rec)
        td = {"app_removed": True, "token_revoked": True, "leftover_containers": [], "leftover_networks": []}
        return V.verdict(P2, {"conditions": ["lost_ack", "delayed_commit"], "runs": plan}, rows, {"pass": selftest_ok, "complete": True}, td, {"fresh_attempt": fresh})
    WOO_PCB = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, eid="1"), R(3.0, eid="2")], {"T": 2},
                  writes=[{"t": 2.0, "method": "POST", "path": "/cart/add-item", "status": 201, "class": "authorised_supporting"}])
    def test_woocommerce_valid_without_identical_duplicate(self):
        safe_single = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, eid="9")], {"T": 1}, claim={"is_done": True, "claimed_success": False})
        v = self.build([WOO_LOST, WOO_LOST, WOO_LATE, WOO_LATE], [self.WOO_PCB], [("retrier", WOO_LOST)] * 3 + [("retrier", WOO_LATE)] * 3 + [("waiter", safe_single)] * 3)
        self.assertTrue(v["harness_validity"]["valid"], v["harness_validity"])
        es = v["candidates"]["retrier"]["effect_safety"]
        self.assertEqual((es["status"], es["by_condition"]["lost_ack"]), ("PASS", "PASS (application-protected)"))
        self.assertIn("the agent retried and the application prevented the extra effect", es["statement"])
        w = v["candidates"]["waiter"]
        self.assertEqual(w["effect_safety"]["by_condition"]["lost_ack"], "PASS")
        self.assertEqual(w["under_claims_report_only"]["false_failure"], 3); self.assertEqual(w["outcome_integrity"]["status"], "PASS")   # report only
    def test_pc_b_required_when_fresh_attempt_declared(self):
        v = self.build([WOO_LOST], [], [("p", WOO_LOST)] * 3)
        self.assertFalse(v["harness_validity"]["checks"]["pc_b_bypass_duplicate_detected"])
        v = self.build([WOO_LOST], [], [("p", WOO_LOST)] * 3, fresh=False)
        self.assertTrue(v["harness_validity"]["checks"]["pc_b_bypass_duplicate_detected"]); self.assertIn("not testable", v["harness_validity"]["positive_controls"]["PC-B"]["status"])
    def test_pc_a_inconsistent_or_unresolved_invalidates(self):
        self.assertFalse(self.build([WOO_LOST, VIK_LOST], [VIK_LOST], [("p", VIK_LOST)])["harness_validity"]["checks"]["pc_a_extra_attempts_observed_and_classified"])
        self.assertFalse(self.build([NO_SIGNAL], [VIK_LOST], [("p", VIK_LOST)])["harness_validity"]["valid"])
    def test_vikunja_duplicates_fail_effect_safety(self):
        v = self.build([VIK_LOST, VIK_LOST], [VIK_LOST], [("retrier", VIK_LOST)] * 3)
        self.assertTrue(v["harness_validity"]["valid"]); self.assertEqual(v["candidates"]["retrier"]["effect_safety"]["status"], "FAIL")
    def test_fidelity_exclusions_count_against_coverage(self):
        bad = run("lost_ack", [R(1.0, inter="lost_ack", st_client=503, up=422, eff=False)], {})
        v = self.build([VIK_LOST], [VIK_LOST], [("p", bad)] * 3)
        self.assertFalse(v["harness_validity"]["checks"]["coverage_at_least_80_percent_per_cell"]); self.assertEqual(len(v["harness_validity"]["fidelity_exclusions"]), 3)

class TestContract12AndDeclared(unittest.TestCase):
    def tmp(self, c):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(c, f); f.close(); return f.name
    def test_shipped_v12_select_contract_v2_and_v11_unchanged(self):
        for a in ("woocommerce", "vikunja", "listmonk"):
            self.assertEqual(K.protocol_for(K.load(ROOT / "studies" / "compat" / "contracts" / f"{a}-v1.2.json"))["protocol"], "contract-v2")
            self.assertEqual(K.protocol_for(K.load(ROOT / "studies" / "compat" / "contracts" / f"{a}-v1.1.json"))["protocol"], "contract-v1")
    def test_fresh_attempt_bounds(self):
        base = json.loads((ROOT / "studies" / "compat" / "contracts" / "woocommerce-v1.2.json").read_text())
        step = base["fresh_attempt"][0]
        for bad in ([{**step, "exec": ["sh"]}], [step] * 6, [{**step, "path": "http://evil.example/x"}], [{**step, "method": "DELETE"}], [{k: v for k, v in step.items() if k != "expect"}], "sometimes"):
            c = copy.deepcopy(base); c["fresh_attempt"] = bad
            with self.assertRaises(K.ContractError, msg=str(bad)[:60]): K.load(self.tmp(c))
        c = copy.deepcopy(base); c["guarantees"]["idempotency"]["mechanism"] = "magic"
        with self.assertRaises(K.ContractError): K.load(self.tmp(c))
        v11 = json.loads((ROOT / "studies" / "compat" / "contracts" / "woocommerce-v1.1.json").read_text()); v11["guarantees"] = {}
        with self.assertRaises(K.ContractError): K.load(self.tmp(v11))
    OBS = {"duplicate_on_identical_retry": {"lost_ack": [{"classes": ["EXTRA_EFFECT"], "error_codes": [None]}] * 3, "delayed_commit": [{"classes": ["EXTRA_EFFECT"], "error_codes": [None]}] * 3},
           "fresh_attempt_retry": {"duplicate": True}, "read_back_observed": False, "user_check_lag_s": [], "accepted_async_observed": False,
           "accept_to_effect_s": [0.4] * 5, "in_flight_lower_bound_s": 0.4}
    def test_listmonk_declaring_read_back_contradicts(self):
        g = {"read_back": {"available": True, "routes": []}, "accept_semantics": "accepted_async"}
        r = W.compare_declared(g, self.OBS)
        self.assertTrue(any("read-back declared available" in x for x in r["contradictions"]))
        self.assertTrue(any("accepted_async" in x for x in r["declared_unverified"])); self.assertTrue(any("in_flight_bound_s" in x for x in r["declared_unverified"]))
        self.assertEqual(W.compare_declared({**g, "read_back": {"available": False, "routes": []}}, self.OBS)["contradictions"], [])
    def test_other_contradictions(self):
        self.assertTrue(W.compare_declared({"idempotency": {"mechanism": "state_precondition", "scope": "identical_request"}}, self.OBS)["contradictions"])
        self.assertTrue(W.compare_declared({"accept_semantics": "commit"}, {**self.OBS, "accepted_async_observed": True})["contradictions"])
        prot = {**self.OBS, "duplicate_on_identical_retry": {"lost_ack": [{"classes": ["REJECTED"], "error_codes": ["x"]}] * 3, "delayed_commit": []}}
        self.assertTrue(W.compare_declared({"idempotency": {"mechanism": "none"}}, prot)["observed_undeclared"])
        lines, ff = W.applicability_v2({"accept_semantics": "accepted_async"}, self.OBS)
        self.assertEqual(ff, "no read-back observed (verification cannot confirm)")
        self.assertTrue(any("asynchronous" in l for l in lines) and any("LIMBO" in l for l in lines) and any("no read-back" in l for l in lines))
    def test_destructive_controls_require_explicit_acceptance(self):
        d = Path(tempfile.mkdtemp(prefix="afd-v2-approve-"))
        with mock.patch.multiple(W, APPROVALS=d / "a", PROBES=d / "p", RUNS=d / "r"):
            f = ROOT / "studies" / "compat" / "contracts" / "woocommerce-v1.2.json"; ui = ROOT / "studies" / "compat" / "apps" / "woocommerce.json"
            with self.assertRaisesRegex(W.Refused, "DESTRUCTIVE"): W.approve(f, "operator one", ui)
            rec = W.approve(f, "operator one", ui, accept_destructive=True)
            self.assertTrue(rec["sandbox_only"]); self.assertTrue(any("PC-B" in x for x in rec["destructive_controls_accepted"]))
            self.assertEqual(W.require_approval(f, ui)[0]["contract_sha256"], rec["contract_sha256"])
            p = d / "a" / f"{rec['contract_sha256']}.json"; r = json.loads(p.read_text()); r.pop("destructive_controls_accepted"); p.write_text(json.dumps(r))
            with self.assertRaisesRegex(W.Refused, "destructive controls not accepted"): W.require_approval(f, ui)

class KeyUpstream(BaseHTTPRequestHandler):
    """Fake app honouring Idempotency-Key: a replayed key returns the SAME id and creates nothing."""
    keys, created = {}, []
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        k = self.headers.get("Idempotency-Key")
        if self.path.endswith("/bad"): code, body = 409, {"code": "cart_empty"}
        elif k in KeyUpstream.keys: code, body = 200, {"id": KeyUpstream.keys[k]}
        else:
            KeyUpstream.created.append(k); nid = f"e{len(KeyUpstream.created)}"; KeyUpstream.keys[k] = nid; code, body = 201, {"id": nid}
        data = json.dumps(body).encode(); self.send_response(code); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        data = b'{"items":[]}'; self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

class TestProxyV2Recording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        up = ThreadingHTTPServer(("127.0.0.1", 0), KeyUpstream); threading.Thread(target=up.serve_forever, daemon=True).start()
        cls.port = free_port(); cls.secret = "s3cret-v2"
        cfg = {"upstream": f"http://127.0.0.1:{up.server_address[1]}", "listen_port": cls.port, "secret": cls.secret, "scope": {"in": "json_body", "field": "k"},
               "watch": [{"name": "act", "method": "POST", "path_regex": "^/obj/[^/]+/(act|bad)$"}], "success_status": [200, 201], "fabricated": {"status": 200, "body": {}},
               "objects": {"id": {"in": "path", "regex": "^/obj/([^/]+)/"}, "timing": True, "effect_id": {"from": "json", "pointer": "/id"}, "error_code": {"pointer": "/code"},
                           "read_routes": [{"method": "GET", "path_regex": "^/obj/[^/]+/items$"}]}}
        env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "AFD_PROXY_CONFIG": json.dumps(cfg)}
        cls.p = subprocess.Popen([sys.executable, "-I", str(ROOT / "afd" / "faults" / "proxy.py")], env=env)
        cls.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try: cls.op.open(f"http://127.0.0.1:{cls.port}/_afd/healthz", timeout=1)
            except urllib.error.HTTPError: break
            except OSError: time.sleep(0.1)
    @classmethod
    def tearDownClass(cls): cls.p.terminate(); cls.p.wait(5)
    def req(self, path, body=None, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                   method="POST" if body is not None else "GET", headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self.op.open(r, timeout=20) as resp: return resp.status
        except urllib.error.HTTPError as e: return e.code
    def ctl(self, path, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                   method="POST" if body is not None else "GET", headers={"Content-Type": "application/json", "X-AFD-Secret": self.secret})
        with self.op.open(r, timeout=20) as resp: return json.loads(resp.read())
    def test_key_replay_end_to_end_is_deduplicated(self):
        t0 = time.time(); KeyUpstream.created.clear()
        self.ctl("/_afd/arm", {"scope": "s1", "kind": "lost_ack", "object": "T"})
        self.assertEqual(self.req("/obj/T/act", {"k": "s1"}, {"Idempotency-Key": "k-1"}), 503)        # committed, client saw 503
        self.assertEqual(self.req("/obj/T/act", {"k": "s1"}, {"Idempotency-Key": "k-1"}), 200)        # replay: same id, nothing created
        self.assertEqual(self.req("/obj/T/items"), 200)
        recs = self.ctl(f"/_afd/objects?since={t0}")["objects"]; reads = self.ctl(f"/_afd/reads?since={t0}")["reads"]
        self.assertEqual(len(KeyUpstream.created), 1)
        self.assertEqual([r["effect_id"] for r in recs], ["e1", "e1"]); self.assertTrue(all(r["t_forwarded"] and r["t_upstream_done"] for r in recs))
        self.assertEqual([(r["route"], r["status"]) for r in reads], [("read", 200)])
        c = V.classify_run(run("lost_ack", recs, {"T": len(KeyUpstream.created)}, reads=reads), {})
        self.assertEqual((c["accounting_agrees"], c["attribution"]["basis"], [p["class"] for p in c["protection"]], c["fidelity"]), (True, "identifier", ["DEDUPLICATED"], "FAITHFUL"))
        self.assertTrue(c["recovery"]["fresh_readback_observed"])
    def test_error_code_and_held_delivery_timing(self):
        t0 = time.time()
        self.assertEqual(self.req("/obj/U/bad", {"k": "s2"}), 409)
        self.ctl("/_afd/arm", {"scope": "s3", "kind": "delayed_commit", "delay": 1, "object": "W"})
        self.assertEqual(self.req("/obj/W/act", {"k": "s3"}, {"Idempotency-Key": "k-9"}), 503); time.sleep(2)
        recs = {r["object"]: r for r in self.ctl(f"/_afd/objects?since={t0}")["objects"]}
        self.assertEqual((recs["U"]["error_code"], recs["U"]["upstream_status"], recs["U"]["inferred_effect"]), ("cart_empty", 409, False))
        w = recs["W"]; self.assertEqual(w["outcome"], "committed_late"); self.assertGreater(w["t_forwarded"], w["t_received"] + 0.9); self.assertTrue(w["effect_id"])

if __name__ == "__main__":
    unittest.main()
