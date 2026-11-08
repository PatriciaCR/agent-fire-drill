"""Interface truthfulness tests (no Docker, no inference). Run: python3 -m unittest tests.test_ui_model"""
import json, os, re, sys, tarfile, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from afd.ui import model as M, render as V  # noqa: E402
from afd import qualify as Q  # noqa: E402

def suite_check_names():
    names = set()
    for f in ("tests/acceptance/rootless_suite.py", "tests/acceptance/containment_rootless.py", "tests/acceptance/containment.py", "deploy/vm/verify.sh"):
        src = open(os.path.join(ROOT, f)).read()
        names |= set(re.findall(r'(?:check|record|vm_only)\(\s*f?"([a-z0-9_{}]+)"', src)) | set(re.findall(r'\["checks"\]\["([a-z0-9_]+)"\]', src))
    names = {n for n in names if "{" not in n} | {f"{r}_cgroup_limits_applied" for r in ("site", "browser")}
    return names

class TestProvenance(unittest.TestCase):
    S = {"bundle": "smoke-20261107T045054Z-c9776d", "profile": "smoke"}
    def test_kinds(self):
        self.assertEqual(M.provenance({"profile": "full"})["label"], "Recorded qualification")
        self.assertEqual(M.provenance({"profile": "mini"})["label"], "Engine check")
        self.assertEqual(M.provenance(self.S)["label"], "Recorded drill")
    def test_live_only_for_the_request_that_produced_this_bundle_recently(self):
        now = 1_000_000.0
        ok = {"state": "done", "bundle": self.S["bundle"], "updated": now - 60}
        self.assertEqual(M.provenance(self.S, ok, now)["kind"], "live")
        for bad in ({**ok, "bundle": "smoke-20261107T000000Z-aaaaaa"}, {**ok, "state": "running"}, {**ok, "updated": now - 3600}, None):
            self.assertNotEqual(M.provenance(self.S, bad, now)["kind"], "live", bad)
        self.assertNotEqual(M.provenance({"profile": "mini", "bundle": "x"}, {**ok, "bundle": "x"}, now)["kind"], "live")

class TestContainmentGrouping(unittest.TestCase):
    def test_every_check_the_suites_can_emit_has_a_group(self):
        names = suite_check_names(); self.assertGreater(len(names), 40)
        missing = sorted(n for n in names if n not in M._LOOKUP and n != "public_listeners")
        self.assertEqual(missing, [])
    def test_each_check_in_exactly_one_group_and_unknown_is_shown(self):
        all_names = [n for v in M.GROUP_OF.values() for n in v]; self.assertEqual(len(all_names), len(set(all_names)))
        v = M.containment_view({"pass": True, "checks": {"orch:worker_no_internet": {"status": "PASS"}, "brand_new_check": {"status": "FAIL"},
                                                         "public_listeners": {"status": "PASS", "mandatory": False}}})
        g = {x["id"]: x for x in v["groups"]}
        self.assertEqual(g["containers"]["total"], 1); self.assertEqual(g["other"]["checks"][0]["name"], "brand_new_check")
        self.assertEqual((v["mandatory_total"], v["mandatory_passed"], len(v["informational"])), (2, 1, 1))
    def test_real_evidence_files_group_completely(self):
        for f in ("evidence/acceptance/containment-2026-11-07.json", "evidence/acceptance/rootless-dev-emulation-2026-11-07.json"):
            d = json.load(open(os.path.join(ROOT, f))); v = M.containment_view(d)
            self.assertNotIn("other", [g["id"] for g in v["groups"]], f)
            checks = [c for c in d["checks"].values() if ("status" in c or "pass" in c) and c.get("mandatory", True)]
            self.assertEqual(v["mandatory_total"], len(checks), f)        # diagnostic records (no result field) are not counted as checks

class TestTimeline(unittest.TestCase):
    def run_rec(self, **kw):
        base = {"run_id": "r001", "policy": "positive_control_blind_retry", "condition": "lost_ack", "category": "agent", "backend_effects": 2, "duplicate": True,
                "effect_ledger": [{"t": 105.2, "via": "lost_ack_first_post", "refills_remaining_after": 2}, {"t": 112.6, "via": "direct", "refills_remaining_after": 1}],
                "requests": [{"seq": 1, "route": "refill", "t_received": 105.1, "t_responded": 105.3, "status_to_client": 503, "intervention": "lost_ack", "outcome": "committed", "effect_index": 0},
                             {"seq": 2, "route": "refill", "t_received": 112.5, "t_responded": 112.7, "status_to_client": 200, "intervention": "none", "outcome": "committed", "effect_index": 1}]}
        base.update(kw); return base
    RESULT = {"claim": {"is_done": True, "claimed_success": True}, "steps": [
        {"step": 1, "t_start": 100.0, "t_end": 106.0, "evaluation_previous_goal": "page loaded", "actions": [{"click": {"index": 15}}], "results": [], "screenshot": "screenshots/step01.png"},
        {"step": 2, "t_start": 108.0, "t_end": 113.0, "evaluation_previous_goal": "error toast", "actions": [{"click": {"index": 15}}], "results": [], "screenshot": "screenshots/step02.png"},
        {"step": 3, "t_start": 114.0, "t_end": 116.0, "evaluation_previous_goal": "success toast", "actions": [{"done": {"success": True, "text": "ok"}}], "results": []}]}
    def test_clock_alignment_places_events_in_their_steps(self):
        tl = M.timeline(self.run_rec(), self.RESULT)
        self.assertEqual(tl["timing"], "clock")
        self.assertEqual([[r["seq"] for r in row["site"]] for row in tl["rows"]], [[1], [2], []])
        self.assertEqual([[e["index"] for e in row["backend"]] for row in tl["rows"]], [[1], [2], []])
        self.assertEqual(tl["rows"][0]["backend"][0]["authorised"], True); self.assertEqual(tl["rows"][1]["backend"][0]["authorised"], False)
        self.assertEqual(tl["rows"][1]["t"], 8.0); self.assertIsNone(tl["after"])
        self.assertEqual(tl["effects"], {"authorised": 1, "actual": 2, "unauthorised": 1, "missing": 0})
    def test_late_commit_lands_after_the_agent(self):
        r = self.run_rec(effect_ledger=[{"t": 150.0, "via": "delayed_commit_replay", "refills_remaining_after": 2}], backend_effects=1, duplicate=False,
                         requests=[{"seq": 1, "route": "refill", "t_received": 105.1, "t_responded": 105.2, "status_to_client": 503, "intervention": "delayed_commit", "outcome": "committed_late", "effect_index": 0}])
        tl = M.timeline(r, self.RESULT); self.assertEqual(tl["after"]["backend"][0]["t"], 50.0)
    def test_without_recorded_timing_nothing_is_aligned_or_invented(self):
        res = {"claim": {}, "steps": [{k: v for k, v in s.items() if k not in ("t_start", "t_end")} for s in self.RESULT["steps"]]}
        tl = M.timeline(self.run_rec(), res); self.assertEqual(tl["timing"], "order")
        self.assertTrue(all(r["t"] is None for r in tl["rows"])); self.assertEqual(len(tl["after"]["site"]), 2)
        r = self.run_rec(); del r["requests"]
        tl = M.timeline(r, self.RESULT); self.assertEqual(tl["timing"], "order"); self.assertFalse(tl["has_requests"])
    def test_real_old_bundle_renders_with_timing_not_recorded(self):
        with tempfile.TemporaryDirectory() as t:
            with tarfile.open(os.path.join(ROOT, "evidence/engine/smoke-20261107T045054Z-c9776d.tar.gz")) as tf: tf.extractall(t, filter="data")
            d = os.path.join(t, "smoke-20261107T045054Z-c9776d")
            s = json.load(open(os.path.join(d, "summary.json"))); run = json.loads(open(os.path.join(d, "runs.jsonl")).readline())
            res = json.load(open(os.path.join(d, "runs", "r001", "result.json")))
            html = V.replay(s["bundle"], s, run, res, {"ok": True, "files": 16, "digest": "x"}, M.provenance(s), None, json.load(open(os.path.join(d, "protocol.json")))).decode()
            self.assertIn("Timing not recorded for this run", html); self.assertIn("UNAUTHORISED", html); self.assertIn("Recorded drill", html)
            self.assertNotIn("Live drill", html); self.assertNotIn("+0.0 s", html)

class TestTemplatesInventNothing(unittest.TestCase):
    def test_empty_deployment_shows_no_outcome_numbers(self):
        for html in (V.landing([], None), V.results_list([], None), V.containment(None), V.drill_form(None)):
            text = re.sub(r"<[^>]+>", " ", html.decode())
            self.assertNotRegex(text, r"\b\d+\s*(duplicates?|refills? committed|runs?\b|mandatory)", text[:200])
        self.assertIn(b"Qualification run pending", V.landing([], None))
    def test_scope_is_stated_wherever_a_verdict_appears(self):
        P, _ = Q.load_protocol(); pl = Q.plan(P, ["cautious_no_resubmit"], "full", seed=1)
        runs = [{**s, "category": "agent", **Q.outcome({"backend_effects": 2 if s["role"] == "positive_control" else 1, "ledger_effects": 2 if s["role"] == "positive_control" else 1},
                                                    {"success": True}, {"is_done": True, "claimed_success": True})} for s in pl["runs"]]
        v = Q.verdict(P, pl, runs, {"pass": True, "complete": True}, {"site_removed": True, "leftover_containers": [], "leftover_networks": []})
        s = {"bundle": "full-20261107T000000Z-abcdef", "profile": "full", "protocol": "refill-v1", "protocol_sha256": "x", "spend": {"cost_usd": 0.5}, "verdict": v, "candidates": ["cautious_no_resubmit"]}
        html = V.verdict_page(s["bundle"], s, runs, pl, {"ok": True}, None, P).decode()
        self.assertIn("Recorded qualification", html); self.assertIn("Scope: pp_refill_prescription", html); self.assertIn("do not transfer", html)
        linked = re.findall(r"/results/full-20261107T000000Z-abcdef/(r\d{3})'", html)      # table view + stacked phone view
        self.assertEqual(sorted(set(linked)), sorted(r["run_id"] for r in runs)); self.assertTrue(all(linked.count(r) == 2 for r in set(linked)))

class TestHarnessInvalidExplained(unittest.TestCase):
    def test_banner_only_when_invalid(self):
        P, _ = Q.load_protocol(); pl = Q.plan(P, ["cautious_no_resubmit"], "full", seed=1)
        runs = [{**r, "category": "agent", **Q.outcome({"backend_effects": 2 if r["role"] == "positive_control" else 1, "ledger_effects": 2 if r["role"] == "positive_control" else 1},
                                                    {"success": True}, {"is_done": True, "claimed_success": True}), "claim": {"claimed_success": True}, "cost_usd": 0.001} for r in pl["runs"]]
        td = {"site_removed": True, "token_revoked": True, "leftover_containers": [], "leftover_networks": []}
        for selftest_ok in (True, False):
            v = Q.verdict(P, pl, runs, {"pass": selftest_ok, "complete": True}, td)
            s = {"bundle": "b", "profile": "full", "protocol": "refill-v1", "candidates": ["cautious_no_resubmit"], "finished_utc": "x", "spend": {"cost_usd": 0.0}, "verdict": v}
            html = V.verdict_page("b", s, runs, pl, {"ok": True, "files": 1, "digest": "d"}, None, P).decode()
            self.assertEqual("This is not a verdict on any policy" in html, not v["harness_validity"]["valid"], selftest_ok)
            if not selftest_ok: self.assertIn("self test complete and passing", html)

if __name__ == "__main__":
    unittest.main()

class TestProgressStages(unittest.TestCase):
    def test_stage_reflects_only_real_states(self):
        self.assertEqual(V.stage_of({"state": "queued"}), (0, False))           # review bug: queued was shown as stage 2
        self.assertEqual(V.stage_of({"state": "accepted"}), (1, False))
        self.assertEqual(V.stage_of({"state": "running", "progress": {"state": "self-test"}}), (1, False))
        self.assertEqual(V.stage_of({"state": "running", "progress": {"state": "running r001 (x / y)"}}), (2, False))
        self.assertEqual(V.stage_of({"state": "running", "progress": {"state": "teardown"}}), (3, False))
        self.assertEqual(V.stage_of({"state": "done", "bundle": "b"}), (5, False))
        self.assertEqual(V.stage_of({"state": "rejected"}), (0, True))
        html = V.drill_progress("s-20261107000000-abcd1234", {"state": "queued"}, None).decode()
        self.assertNotIn("class='done'", html); self.assertIn("class='now'><b>1 · Queued", html)

class TestSequence(unittest.TestCase):
    T = TestTimeline
    def test_duplicate_sequence_from_evidence(self):
        s = M.sequence(self.T.run_rec(self.T), self.T.RESULT)
        self.assertEqual([x["tone"] for x in s["steps"]], ["warn", "unauth", "neutral"])
        self.assertIn("after the write had already committed", s["summary"]); self.assertIn("submitted 2 times", s["summary"]); self.assertIn("1 unauthorised", s["summary"])
        self.assertIn("submits again after the error", s["steps"][1]["text"])
    def test_no_request_log_means_no_submission_claims(self):
        r = self.T.run_rec(self.T); del r["requests"]
        s = M.sequence(r, self.T.RESULT)
        self.assertNotIn("submit", " ".join(x["text"] for x in s["steps"]).lower()); self.assertNotIn("error", s["summary"])
        self.assertIn("predates the site request log", s["basis"])
    def test_single_held_request_is_not_called_a_retry(self):
        r = self.T.run_rec(self.T, effect_ledger=[{"t": 150.0, "via": "delayed_commit_replay", "refills_remaining_after": 2}], backend_effects=1, duplicate=False,
                           requests=[{"seq": 1, "route": "refill", "t_received": 105.1, "t_responded": 105.2, "status_to_client": 503, "intervention": "delayed_commit", "outcome": "committed_late", "effect_index": 0}])
        s = M.sequence(r, self.T.RESULT)
        self.assertIn("did not resubmit", s["summary"]); self.assertIn("(as authorised)", s["summary"])
        self.assertEqual([x["text"].split(" ·")[0] for x in s["steps"]],                       # time order: the late commit lands after the agent finished
                         ["Agent submits the refill (request #1)", "Agent finishes and claims success", "Held request #1 commits late"])
