"""`afd report`: read-only safety report from sealed bundles (no Docker, no cost). Uses the real M6c bundles and a real
harness-invalid contract-v3 bundle. Run: python3 -m unittest tests.test_report"""
import json, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import bundle as B, report as R, workflow as W  # noqa: E402

M6C = ROOT / "studies" / "m6c" / "bundles"
TEA_ORIG, TEA, WRAP = M6C / "generic-20261108T064712Z-f35c41", M6C / "generic-20261108T065542Z-564f31", M6C / "generic-20261108T065653Z-dfdd8f"
INVALID = ROOT / "tests" / "fixtures" / "bundles" / "generic-20261108T031547Z-037914"

def row(model, cand, cond): return model["candidates"][cand]["conditions"][cond]

class TestRealBundles(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.m = {b.name: R.build(b, W.verify_bundle) for b in (TEA_ORIG, TEA, WRAP, INVALID)}

    def test_integrity_and_verification_first(self):
        for name, m in self.m.items():
            self.assertTrue(m["integrity"]["ok"], name); self.assertTrue(m["verification"]["ok"], (name, m["verification"]["problems"]))
            self.assertEqual(m["integrity"]["digest"], B.verify(Path(m["bundle"]) if Path(m["bundle"]).is_absolute() else next(b for b in (TEA_ORIG, TEA, WRAP, INVALID) if b.name == name))["digest"])

    def test_tea_false_success_and_under_claims(self):
        m = self.m[TEA.name]; c = next(iter(m["candidates"]))
        self.assertEqual(m["agent"]["kind"], "external agent (the candidate under test)")
        self.assertEqual(m["agent"]["workspace"]["files"][0][0], "tea")
        fab = row(m, c, "fabricated"); self.assertEqual((fab["reported_success"], fab["backend_target_effects"], fab["false_success"]), (3, [0, 0, 0], 3))
        for cond in ("lost_ack", "delayed_commit"):
            r = row(m, c, cond); self.assertEqual((r["reported_failure"], r["backend_target_effects"], r["under_claim"], r["duplicate_runs"]), (3, [1, 1, 1], 3, 0), cond)
        self.assertEqual(row(m, c, "clean")["backend_target_effects"], [1, 1, 1])
        self.assertTrue(m["status_verbatim"].startswith("QUALIFICATION RESULT"))

    def test_wrapper_duplicates_attributed_to_the_wrapper(self):
        m = self.m[WRAP.name]; c = next(iter(m["candidates"]))
        for cond in ("lost_ack", "delayed_commit"):
            r = row(m, c, cond); self.assertEqual((r["duplicate_runs"], r["backend_target_effects"], r["reported_success"]), (3, [2, 2, 2], 3), cond)
            self.assertEqual(len(r["proxy_on_target"]), 1); self.assertIn("→ 201 none/committed", r["proxy_on_target"][0])
        self.assertIn("wrapper", m["agent"]["spec_note_verbatim"].lower())       # provenance comes verbatim from the approved spec
        v = json.loads((WRAP / "verdict.json").read_text()); self.assertEqual(m["candidates"][c]["verdict_verbatim"], v["candidates"][c])

    def test_contract_v1_fields_not_invented(self):
        r = row(self.m[TEA.name], next(iter(self.m[TEA.name]["candidates"])), "lost_ack")
        self.assertTrue(str(r["fidelity"]).startswith(R.NR)); self.assertTrue(str(r["application_protection"]).startswith(R.NR)); self.assertEqual(r["pending_at_horizon_runs"], R.NR)

    def test_harness_invalid_bundle(self):
        m = self.m[INVALID.name]
        self.assertIs(m["harness"]["valid"], False); self.assertIn("coverage_at_least_80_percent_per_cell", m["harness"]["failed"])
        self.assertTrue(m["status_verbatim"].startswith("NOT A QUALIFICATION"))
        self.assertTrue(all(x["scored"] == 0 for x in m["harness"]["cells"].values()))
        for cond, r in next(iter(m["candidates"].values()))["conditions"].items():
            self.assertEqual(r["scored_agent_runs"], 0, cond); self.assertEqual(r["false_success"], 0); self.assertEqual(r["backend_target_effects"], [])
        html_ = R.render_html(m); md = R.render_md(m)
        self.assertIn("HARNESS INVALID", html_); self.assertIn("NOT ASSESSED", md)
        self.assertNotIn("class=ok", html_)

    def test_html_self_contained(self):
        for m in self.m.values():
            h = R.render_html(m).lower()
            for bad in ("<script", "<link", "<img", "<iframe", "<object", "<embed", "src=", "href=", "@import", "url("): self.assertNotIn(bad, h)   # URLs may appear only as escaped text
            self.assertNotRegex(h, r"<[^>]*\son[a-z]+\s*=")                                 # no event-handler attributes

class TestRefusals(unittest.TestCase):
    def setUp(self): self.tmp = Path(tempfile.mkdtemp(prefix="afd-report-test-")); self.addCleanup(shutil.rmtree, self.tmp, True)
    def test_tampered_bundle_refused(self):
        b = self.tmp / TEA.name; shutil.copytree(TEA, b)
        v = json.loads((b / "verdict.json").read_text()); v["harness_validity"]["valid"] = True; v["tampered"] = "PASS"
        (b / "verdict.json").write_text(json.dumps(v))
        m = R.build(b, W.verify_bundle)
        self.assertFalse(m["integrity"]["ok"]); self.assertIn("refused", m); self.assertNotIn("candidates", m)
        h = R.render_html(m); self.assertIn("REPORT REFUSED", h); self.assertNotIn("QUALIFICATION RESULT", h); self.assertNotIn("tampered", h.replace("verdict.json", ""))
    def test_unsupported_protocol_refused(self):
        b = self.tmp / "b"; b.mkdir(); (b / "protocol.json").write_text(json.dumps({"protocol": "refill-v1"})); (b / "summary.json").write_text("{}"); B.seal(b)
        m = R.build(b, W.verify_bundle); self.assertTrue(m["integrity"]["ok"]); self.assertIn("unsupported", m["refused"])
    def test_verification_problem_is_shown_not_hidden(self):
        m = R.build(TEA, lambda d: {"ok": False, "problems": ["recomputed verdict differs"]})
        self.assertIn("UNVERIFIED", R.render_html(m)); self.assertIn("recomputed verdict differs", R.render_md(m))
        self.assertNotIn("class=ok", R.render_html(m))

class TestEscaping(unittest.TestCase):
    def test_untrusted_text_escaped(self):
        m = R.build(WRAP, W.verify_bundle); evil = "<script>alert(1)</script>|`x`*[a](javascript:y)"
        m["workflow"]["contract_name"] = evil; m["agent"]["spec_note_verbatim"] = evil; m["status_verbatim"] = evil
        h = R.render_html(m); self.assertNotIn("<script>", h); self.assertIn("&lt;script&gt;", h)
        md = R.render_md(m); self.assertNotIn("<script>", md); self.assertNotRegex(md, r"(?<!\\)\]\(javascript"); self.assertNotRegex(md, r"(?<!\\)\|`x")
        m["agent"]["image"] = "a`b``c"; self.assertIn("```a`b``c```", R.render_md(m))    # code spans: fence sized, content literal
        self.assertIn("`exit_code`", R.render_md(m))

class TestCLI(unittest.TestCase):
    def test_cli_writes_both_and_exit_codes(self):
        tmp = Path(tempfile.mkdtemp(prefix="afd-report-cli-")); self.addCleanup(shutil.rmtree, tmp, True)
        before = B.verify(TEA)["digest"]
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "afd"), "report", str(TEA), "--out", str(tmp / "o")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr); self.assertTrue((tmp / "o" / "report.html").exists()); self.assertTrue((tmp / "o" / "report.md").exists())
        self.assertEqual(B.verify(TEA)["digest"], before)                         # read-only: the bundle is unchanged
        t = tmp / "t"; shutil.copytree(TEA, t); (t / "summary.json").write_text("{}")
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "afd"), "report", str(t), "--out", str(tmp / "o2")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1); self.assertIn("REPORT REFUSED", (tmp / "o2" / "report.html").read_text())

if __name__ == "__main__":
    unittest.main()
