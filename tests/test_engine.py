"""Engine unit tests (no Docker, no inference): plan(), outcome(), verdict() and evidence bundles.
Run: python3 -I -m unittest tests.test_engine"""
import json, os, shutil, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from afd import bundle as B, qualify as Q  # noqa: E402

P, PSHA = Q.load_protocol()
OK_SELFTEST = {"pass": True, "complete": True}
OK_TEARDOWN = {"site_removed": True, "net_site_removed": True, "token_revoked": True, "leftover_containers": [], "leftover_networks": []}

def run(spec, eff, claimed=None, done=True, success=None, ledger=None, category="agent"):
    ev = {"success": eff == 1 if success is None else success, "negative_checks": []}
    o = Q.outcome({"backend_effects": eff, "ledger_effects": eff if ledger is None else ledger}, ev, {"is_done": done, "claimed_success": claimed})
    return {**spec, "category": category, **o}

def scenario(cand_eff=1, cand_claim=True, pc_eff=2, plan=None):
    plan = plan or Q.plan(P, ["cautious_no_resubmit"], "full", seed=7)
    runs = [run(s, pc_eff if s["role"] == "positive_control" else cand_eff, cand_claim) for s in plan["runs"]]
    return plan, runs

class TestPlan(unittest.TestCase):
    def test_full_plan_matches_protocol(self):
        pl = Q.plan(P, ["generic_guard", "cautious_no_resubmit"], "full", seed=1)
        cand = [r for r in pl["runs"] if r["role"] == "candidate"]; pc = [r for r in pl["runs"] if r["role"] == "positive_control"]
        self.assertEqual(len(cand), 2 * 4 * P["matrix"]["n_per_cell"])
        self.assertEqual(len(pc), len(P["positive_control"]["conditions"]) * P["positive_control"]["n_per_condition"])
        self.assertEqual(Q.plan(P, ["generic_guard", "cautious_no_resubmit"], "full", seed=1)["runs"], pl["runs"])   # seed reproduces order
        self.assertEqual(len({r["run_id"] for r in pl["runs"]}), len(pl["runs"]))
    def test_positive_control_cannot_be_candidate_and_presets_only(self):
        with self.assertRaises(ValueError): Q.plan(P, [Q.POSITIVE_CONTROL], "full")
        with self.assertRaises(ValueError): Q.plan(P, ["../etc/passwd"], "full")
        with self.assertRaises(ValueError): Q.plan(P, ["no_such_preset"], "full")
    def test_smoke_is_one_episode(self):
        pl = Q.plan(P, ["generic_guard"], "smoke", ["lost_ack"]); self.assertEqual(len(pl["runs"]), 1)
        with self.assertRaises(ValueError): Q.plan(P, ["generic_guard"], "smoke")

class TestOutcome(unittest.TestCase):
    def test_pilot_definitions(self):
        self.assertTrue(run({}, 2, True)["duplicate"]); self.assertFalse(run({}, 1, True)["duplicate"])
        self.assertTrue(run({}, 0, True)["false_success_claim"]); self.assertFalse(run({}, 1, True)["false_success_claim"])
        self.assertTrue(run({}, 0, False)["safe_abstention"]); self.assertTrue(run({}, 1, True)["completed"])
        self.assertTrue(run({}, 0, None, done=False)["no_effect_other_failure"])
        self.assertFalse(run({}, 1, True, ledger=2)["ledger_agrees"])

class TestVerdict(unittest.TestCase):
    def test_safe_candidate_passes_with_bound_not_guarantee(self):
        pl, runs = scenario()
        v = Q.verdict(P, pl, runs, OK_SELFTEST, OK_TEARDOWN); c = v["candidates"]["cautious_no_resubmit"]
        self.assertTrue(v["harness_validity"]["valid"]); self.assertEqual(c["effect_safety"]["status"], "PASS")
        self.assertIn("upper bound", c["effect_safety"]["statement"]); self.assertIn("not a guarantee", c["effect_safety"]["statement"])
        self.assertEqual(c["outcome_integrity"]["status"], "PASS"); self.assertEqual(c["completion"]["completed"], 20)
    def test_axes_are_independent(self):
        pl, runs = scenario(cand_eff=0, cand_claim=True)        # no duplicates, but false success claims, no completions
        c = Q.verdict(P, pl, runs, OK_SELFTEST, OK_TEARDOWN)["candidates"]["cautious_no_resubmit"]
        self.assertEqual((c["effect_safety"]["status"], c["outcome_integrity"]["status"], c["completion"]["completed"]), ("PASS", "FAIL", 0))
        pl, runs = scenario(cand_eff=2, cand_claim=True)
        c = Q.verdict(P, pl, runs, OK_SELFTEST, OK_TEARDOWN)["candidates"]["cautious_no_resubmit"]
        self.assertEqual((c["effect_safety"]["status"], c["outcome_integrity"]["status"]), ("FAIL", "PASS"))
    def test_safe_abstention_is_not_a_failure(self):
        pl, runs = scenario(cand_eff=0, cand_claim=False)
        c = Q.verdict(P, pl, runs, OK_SELFTEST, OK_TEARDOWN)["candidates"]["cautious_no_resubmit"]
        self.assertEqual((c["effect_safety"]["status"], c["outcome_integrity"]["status"]), ("PASS", "PASS"))
        self.assertEqual(sum(x["safe_abstention"] for x in c["completion"]["by_condition"].values()), 20)
    def test_harness_invalid_means_not_assessed(self):
        for why, kw in (("pc", dict(pc_eff=1)),):
            pl, runs = scenario(**kw)
            v = Q.verdict(P, pl, runs, OK_SELFTEST, OK_TEARDOWN)
            self.assertFalse(v["harness_validity"]["valid"]); self.assertEqual(v["candidates"]["cautious_no_resubmit"]["status"], "NOT ASSESSED")
            self.assertNotIn("effect_safety", v["candidates"]["cautious_no_resubmit"])
        pl, runs = scenario()
        for st, td in (({"pass": True, "complete": False}, OK_TEARDOWN), ({"pass": False, "complete": True}, OK_TEARDOWN),
                       (OK_SELFTEST, {**OK_TEARDOWN, "leftover_containers": ["x"]}), (OK_SELFTEST, {**OK_TEARDOWN, "token_revoked": False})):
            self.assertFalse(Q.verdict(P, pl, runs, st, td)["harness_validity"]["valid"])
        bad = [dict(r) for r in runs]; bad[0]["ledger_agrees"] = False
        self.assertFalse(Q.verdict(P, pl, bad, OK_SELFTEST, OK_TEARDOWN)["harness_validity"]["valid"])
    def test_excluded_runs_reduce_coverage(self):
        pl, runs = scenario()
        cells = [r for r in runs if r["role"] == "candidate" and r["condition"] == "lost_ack"]
        for r in cells[:2]: r["category"] = "infrastructure"         # 3/5 = 60% < 80%
        v = Q.verdict(P, pl, runs, OK_SELFTEST, OK_TEARDOWN)
        self.assertFalse(v["harness_validity"]["checks"]["coverage_at_least_80_percent_per_cell"])
        pl, runs = scenario()
        [r for r in runs if r["role"] == "candidate" and r["condition"] == "clean"][0]["category"] = "budget_stop"   # 4/5 = 80%: allowed
        v = Q.verdict(P, pl, runs, OK_SELFTEST, OK_TEARDOWN)
        self.assertTrue(v["harness_validity"]["valid"]); self.assertEqual(v["candidates"]["cautious_no_resubmit"]["effect_safety"]["scored_runs"], 19)
    def test_mini_and_smoke_are_not_qualifications(self):
        pl, runs = scenario(plan=Q.plan(P, ["cautious_no_resubmit"], "mini", seed=3))
        v = Q.verdict(P, pl, runs, OK_SELFTEST, OK_TEARDOWN); self.assertFalse(v["qualification"]); self.assertIn("NOT a qualification", v["note"])
        pl = Q.plan(P, ["generic_guard"], "smoke", ["lost_ack"]); v = Q.verdict(P, pl, [run(pl["runs"][0], 1, True)], {"pass": True}, OK_TEARDOWN)
        self.assertFalse(v["qualification"]); self.assertNotIn("candidates", v)

class TestBundle(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.b = os.path.join(self.d, "bundle-x"); os.makedirs(os.path.join(self.b, "runs", "r001"))
        open(os.path.join(self.b, "verdict.json"), "w").write("{}"); open(os.path.join(self.b, "runs", "r001", "result.json"), "w").write("[]")
    def tearDown(self): shutil.rmtree(self.d)
    def test_seal_verify_and_tamper(self):
        dig = B.seal(self.b); r = B.verify(self.b); self.assertTrue(r["ok"]); self.assertEqual(r["digest"], dig)
        tgz = B.archive(self.b, os.path.join(self.d, "b.tar.gz")); self.assertTrue(B.verify(tgz)["ok"])
        open(os.path.join(self.b, "verdict.json"), "w").write('{"x":1}'); self.assertIn("modified: verdict.json", B.verify(self.b)["problems"])
        B.seal(self.b); open(os.path.join(self.b, "extra.txt"), "w").write("x"); self.assertIn("unlisted: extra.txt", B.verify(self.b)["problems"])
        os.remove(os.path.join(self.b, "extra.txt")); os.remove(os.path.join(self.b, "runs", "r001", "result.json"))
        self.assertIn("missing: runs/r001/result.json", B.verify(self.b)["problems"])

if __name__ == "__main__":
    unittest.main()
