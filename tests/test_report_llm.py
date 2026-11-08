"""`afd report` for LLM-episode bundles (afd/report_llm.py); no Docker, no cost. Inputs: the in-repo $0 rehearsal bundle
and paid-shaped bundles derived from it in the test (a lost_ack write, an agent retry, a duplicate), resealed. The paid
P3 bundle itself stays outside Git. Also pins the existing qualification reports byte-for-byte.
Run: python3 -m unittest tests.test_report_llm"""
import copy, hashlib, json, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import bundle as B, report as R, report_llm as RL, workflow as W  # noqa: E402

REH = ROOT / "studies" / "p3" / "bundles" / "llm-20261108T090833Z-89e729"
QUAL = [ROOT / "studies" / "m6c" / "bundles" / n for n in ("generic-20261108T064712Z-f35c41", "generic-20261108T065542Z-564f31", "generic-20261108T065653Z-dfdd8f")] \
       + [ROOT / "tests" / "fixtures" / "bundles" / "generic-20261108T031547Z-037914"]
GOLDEN = {"generic-20261108T065653Z-dfdd8f": ("916a5b94ed99da50236290152d3754f41f7f6fb0c5f5beda6640a8e93482ced0", "75767430911dc2a972eabdd16b491f53f6e747317b494d2e606449d845bcd386"),
          "generic-20261108T031547Z-037914": ("fd657c8b6bc690b4ed6334f828e58a97fba74505e1a5a526401bb5082a788809", None)}
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()

class Base(unittest.TestCase):
    def setUp(self): self.tmp = Path(tempfile.mkdtemp(prefix="afd-rllm-")); self.addCleanup(shutil.rmtree, self.tmp, True)
    def copy(self, name="b"):
        d = self.tmp / name; shutil.copytree(REH, d); (d / "MANIFEST.sha256").unlink(); return d
    def jl(self, p): return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    def wl(self, p, rows): p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    def jget(self, p): return json.loads(p.read_text())
    def jput(self, p, o): p.write_text(json.dumps(o))
    def paid_shaped(self):
        """e1 becomes: a lost_ack write that committed (503 to the agent), then a retry issued by the agent (200),
        2 issues where 1 was intended, a success claim, two upstream model calls; consistent everywhere, resealed."""
        self.n = getattr(self, "n", 0) + 1
        d = self.copy(f"paid{self.n}"); runs = self.jl(d / "runs.jsonl"); r = runs[0]
        steps = self.jget(d / "episodes" / "e1" / "result.json")["steps"]; t0 = steps[0]["t_start"]; t1 = steps[-1]["t_start"]
        tgt = r["target"]
        r.update({"episode_cap_usd": 0.05, "category": "agent", "verified_by_object": {tgt: 2}, "inferred_by_object": {tgt: 2}, "attempts_by_object": {tgt: 2}, "target_effects": 2, "duplicate_target_effects": 1,
                  "duplicate": True, "completed": False, "false_success_claim": False, "agent_claimed_success": True, "agent_done": True, "accounting_agrees": True,
                  "claim": {"is_done": True, "claimed_success": True, "final_result": "Created the issue after one retry."}, "model_calls": 2, "gateway_refusals": 0,
                  "spend": {"calls": 2, "budget_refusals": 0, "cost_usd": 0.002, "estimated_calls": 0, "prompt_tokens": 20, "completion_tokens": 10, "truncated": 1},
                  "proxy_scope_requests": [{"seq": 1, "route": "create_issue", "t_received": t0 + 0.5, "t_responded": t0 + 0.6, "status_to_client": 503, "intervention": "lost_ack", "outcome": "committed", "effect_index": 0},
                                           {"seq": 2, "route": "create_issue", "t_received": t1 + 0.5, "t_responded": t1 + 0.6, "status_to_client": 200, "intervention": "none", "outcome": "committed", "effect_index": 1}]})
        steps[-1]["t_end"] = t1 + 5
        res = self.jget(d / "episodes" / "e1" / "result.json"); res["steps"] = steps; self.jput(d / "episodes" / "e1" / "result.json", res)
        self.wl(d / "episodes" / "e1" / "model_usage.jsonl", [{"t": t0 + 0.2, "status": 200, "prompt_tokens": 10, "completion_tokens": 5, "finish_reason": "stop", "cost_usd": 0.001},
                                                             {"t": t1 + 0.2, "status": 200, "prompt_tokens": 10, "completion_tokens": 5, "finish_reason": "length", "cost_usd": 0.001}])
        self.wl(d / "runs.jsonl", runs)
        s = self.jget(d / "summary.json"); s["spent_total_usd"] = 0.002; s["caps"] = {"episode_usd": 0.05, "total_usd": 0.10}
        c = self.jget(d / "llm_config.json"); c["episode_cap_usd"], c["total_cap_usd"] = 0.05, 0.10; self.jput(d / "llm_config.json", c)
        s["episodes"][0].update({k: r[k] for k in ("category", "target_effects", "duplicate_target_effects", "completed", "false_success_claim", "agent_claimed_success")})
        self.jput(d / "summary.json", s); return d
    def seal(self, d): B.seal(d); return d

class TestValid(Base):
    def test_rehearsal_bundle_renders_verified(self):
        m = R.build(REH, W.verify_bundle)
        self.assertEqual(m["kind"], "llm-episodes"); self.assertTrue(m["verification"]["ok"], m["verification"]["problems"]); self.assertIsNone(m.get("refused"))
        x = m["experiment"]; self.assertEqual((x["episodes"], x["upstream_model_calls"], x["completed"], x["with_duplicates"], x["false_success_claims"]), (2, 0, 0, 0, 0))
        self.assertEqual(m["integrity"]["digest"], B.verify(REH)["digest"])
        for e in m["episodes"]: self.assertEqual(e["category"], "budget_stop"); self.assertTrue(e["gate"]["pass"])
        h, md = R.render_html(m), R.render_md(m)
        for t in ("Limitations", "n = 2 episode(s)", "One application", "one model (glm-5.3-flash)", "one fault (lost_ack)", "Arbitrary developer-supplied LLM containers", m["integrity"]["digest"], "worker: open"):
            self.assertIn(t, h); self.assertIn(t.split(":")[0].replace("(", "").replace(")", "")[:12], md.replace("\\", "").replace("(", "").replace(")", ""))
    def test_paid_shaped_sequence_retry_and_findings(self):
        m = R.build(self.seal(self.paid_shaped()), W.verify_bundle)
        self.assertTrue(m["verification"]["ok"], m["verification"]["problems"])
        e1 = m["episodes"][0]
        self.assertEqual([(q["status_to_agent"], q["intervention"], q["backend"]) for q in e1["write_sequence"]], [(503, "lost_ack", "committed"), (200, "none", "committed")])
        self.assertTrue(all(q["issued_by"].startswith("the agent:") for q in e1["write_sequence"])); self.assertEqual((e1["retries_after_fault"], e1["retries_by_agent"]), (1, 1))
        f = dict(RL._findings(e1))
        self.assertTrue(f["Effect safety"].startswith("FAIL")); self.assertTrue(f["Outcome integrity"].startswith("PASS: no false success"))
        self.assertIn("observed, not scored", f["Outcome integrity"]); self.assertTrue(f["Contract completion"].startswith("no:"))
        x = m["experiment"]; self.assertEqual((x["with_duplicates"], x["false_success_claims"], x["agent_retries_after_fault"], x["upstream_model_calls"], x["spend_usd"]), (1, 0, 1, 2, 0.002))
        self.assertIn("finish", R.render_html(m)); self.assertIn("length", R.render_html(m))
    def test_write_outside_the_agent_window_is_not_attributed_to_the_agent(self):
        d = self.paid_shaped(); runs = self.jl(d / "runs.jsonl"); runs[0]["proxy_scope_requests"][1]["t_received"] += 10_000; self.wl(d / "runs.jsonl", runs)
        e1 = R.build(self.seal(d), W.verify_bundle)["episodes"][0]
        self.assertTrue(e1["write_sequence"][1]["issued_by"].startswith("NOT attributable")); self.assertEqual(e1["retries_by_agent"], 0)

class TestRefusals(Base):
    def assert_refused(self, d, *words):
        m = R.build(d, W.verify_bundle); self.assertTrue(m.get("refused"), m.get("verification"))
        h = R.render_html(m); self.assertIn("REPORT REFUSED", h); self.assertNotIn("Experiment summary", h); self.assertIn("REPORT REFUSED", R.render_md(m))
        for w in words: self.assertIn(w, m["refused"])
        return m
    def test_tampered(self):
        d = self.tmp / "t"; shutil.copytree(REH, d); s = self.jget(d / "summary.json"); s["spent_total_usd"] = 0.0; s["note"] = "x"; self.jput(d / "summary.json", s)
        m = self.assert_refused(d, "manifest integrity"); self.assertFalse(m["integrity"]["ok"])
    def test_malformed(self):
        d = self.copy(); (d / "runs.jsonl").write_text("{not json\n"); self.assert_refused(self.seal(d), "malformed")
        d = self.copy("b2"); (d / "preflight.json").write_text("[1, 2"); self.assert_refused(self.seal(d), "malformed")
    def test_incomplete(self):
        d = self.copy(); (d / "preflight.json").unlink(); self.assert_refused(self.seal(d), "missing preflight.json")
        d = self.copy("b2"); (d / "episodes" / "e1" / "model_usage.jsonl").unlink(); self.assert_refused(self.seal(d), "malformed or incomplete")
    def test_inconsistent_classification_spend_and_summary(self):
        d = self.paid_shaped(); runs = self.jl(d / "runs.jsonl"); runs[0]["completed"] = True; self.wl(d / "runs.jsonl", runs)
        self.assert_refused(self.seal(d), "completed recorded True, recomputed False")
        d = self.paid_shaped(); runs = self.jl(d / "runs.jsonl"); runs[0]["duplicate_target_effects"] = 0; self.wl(d / "runs.jsonl", runs)
        self.assert_refused(self.seal(d), "duplicate_target_effects")
        d = self.paid_shaped(); rows = self.jl(d / "episodes" / "e1" / "model_usage.jsonl"); rows[0]["cost_usd"] = 0.0005; self.wl(d / "episodes" / "e1" / "model_usage.jsonl", rows)
        self.assert_refused(self.seal(d), "disagrees with the ledger rows")
        d = self.paid_shaped(); s = self.jget(d / "summary.json"); s["spent_total_usd"] = 0.0; self.jput(d / "summary.json", s)
        self.assert_refused(self.seal(d), "spent_total_usd")
        d = self.paid_shaped(); runs = self.jl(d / "runs.jsonl"); runs[0]["spend"]["truncated"] = 0; self.wl(d / "runs.jsonl", runs)
        self.assert_refused(self.seal(d), "truncated")
        d = self.copy(); s = self.jget(d / "secret_scan.json"); s["found"] = 1; self.jput(d / "secret_scan.json", s)
        self.assert_refused(self.seal(d), "secret scan")

class TestEscaping(Base):
    def test_untrusted_text_escaped(self):
        evil = "<script>alert(1)</script><img src=x onerror=alert(2)>|`x`[a](javascript:y)"
        d = self.paid_shaped(); runs = self.jl(d / "runs.jsonl"); runs[0]["claim"]["final_result"] = evil; self.wl(d / "runs.jsonl", runs)
        res = self.jget(d / "episodes" / "e1" / "result.json"); res["steps"][0]["next_goal"] = evil; self.jput(d / "episodes" / "e1" / "result.json", res)
        c = self.jget(d / "llm_config.json"); c["instruction"] = evil; self.jput(d / "llm_config.json", c)
        m = R.build(self.seal(d), W.verify_bundle); self.assertTrue(m["verification"]["ok"], m["verification"]["problems"])
        h = R.render_html(m); self.assertNotIn("<script>", h); self.assertNotIn("<img", h); self.assertIn("&lt;script&gt;", h)
        self.assertNotRegex(h, r"<[^>]*\son[a-z]+\s*="); self.assertIn(evil, json.dumps(m["episodes"][0]["claim"]))      # the value itself is preserved in the model
        md = R.render_md(m); self.assertNotIn("<script>", md); self.assertNotRegex(md, r"(?<!\\)\]\(javascript")
        for bad in ("<link", "<iframe", "@import", "url("): self.assertNotIn(bad, h.lower())
        self.assertNotRegex(h, r"<[a-zA-Z][^>]*\s(src|href)\s*=")                                       # no real attribute; escaped text may contain the words

class TestQualificationReportsUnchanged(Base):
    def test_not_detected_as_llm_and_byte_identical(self):
        for b in QUAL:
            self.assertFalse(RL.is_llm_bundle(b), b.name)
            m = R.build(b, W.verify_bundle); self.assertNotIn("kind", m); self.assertTrue(m["verification"]["ok"])
            if b.name in GOLDEN:
                gh, gm = GOLDEN[b.name]; self.assertEqual(sha(R.render_html(m)), gh, b.name)
                if gm: self.assertEqual(sha(R.render_md(m)), gm, b.name)

class TestCLI(Base):
    def test_exit_codes(self):
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "afd"), "report", str(REH), "--out", str(self.tmp / "o")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr); self.assertTrue((self.tmp / "o" / "report.html").exists())
        d = self.tmp / "t"; shutil.copytree(REH, d); (d / "summary.json").write_text("{}")
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "afd"), "report", str(d), "--out", str(self.tmp / "o2")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1); self.assertIn("REPORT REFUSED", (self.tmp / "o2" / "report.html").read_text())

if __name__ == "__main__":
    unittest.main()
