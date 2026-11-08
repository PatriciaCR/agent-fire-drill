"""P3: real-LLM episodes through the generalized engine (no Docker, no cost; host effects mocked).
  - the worker isolation gate fails closed (real subprocess): an endpoint that must be closed but answers, or one that
    must be open but does not, exits 42 and the worker code is never executed;
  - the browser probe pass rule; isolation targets per position; worker runs refuse a model run without gateway+token;
  - config limits ($0.05/episode, $0.10 total, 2 episodes, existing faults, the gateway's model), the paid confirmation,
    and the episode loop: stop after a gate failure, refuse an episode that could exceed the total cap.
Run: python3 -m unittest tests.test_llm_episodes"""
import copy, json, shutil, socket, subprocess, sys, tempfile, threading, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import config as C, generic_llm as GL, llm_episodes as LE  # noqa: E402

CFG = json.loads((ROOT / "studies" / "p3" / "gitea-ui-llm.json").read_text())

def write_cfg(d, **over):
    c = {**copy.deepcopy(CFG), **over}; p = Path(d) / "cfg.json"; p.write_text(json.dumps(c)); return p

class TestGateFailsClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="afd-gate-")); self.marker = self.tmp / "worker-ran"
        self.then = self.tmp / "worker.py"; self.then.write_text(f"open({str(self.marker)!r}, 'w').close()\n")
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(8); self.port = self.srv.getsockname()[1]
        threading.Thread(target=lambda: [self.srv.accept()[0].close() for _ in range(8)], daemon=True).start()
    def tearDown(self):
        self.srv.close()
        for f in (self.marker, self.then): f.unlink(missing_ok=True)
        self.tmp.rmdir()
    def gate(self, t):
        r = subprocess.run([sys.executable, "-I", "-c", GL.ISOLATION_GATE, json.dumps({**t, "then": str(self.then)})], capture_output=True, text=True, timeout=60)
        return r.returncode, json.loads(r.stdout.split("AFD-GATE:", 1)[1].splitlines()[0])
    def test_reachable_closed_endpoint_stops_the_worker(self):
        rc, g = self.gate({"open": {}, "closed": {"must-be-closed": ["127.0.0.1", self.port]}})
        self.assertEqual(rc, GL.GATE_EXIT); self.assertFalse(g["pass"]); self.assertIn("must-be-closed", g["violations"])
        self.assertFalse(self.marker.exists())                                            # the worker never ran
    def test_unreachable_required_endpoint_stops_the_worker(self):
        rc, g = self.gate({"open": {"must-be-open": ["127.0.0.1", 1]}, "closed": {}})
        self.assertEqual(rc, GL.GATE_EXIT); self.assertIn("must-be-open", g["violations"]); self.assertFalse(self.marker.exists())
    def test_resolving_public_dns_is_a_violation(self):
        rc, g = self.gate({"open": {}, "closed": {}, "dns_closed": ["localhost"]})
        self.assertEqual(rc, GL.GATE_EXIT); self.assertIn("localhost", g["violations"])

class TestBrowserRule(unittest.TestCase):
    GOOD = {"open": {"proxy": "OPEN"}, "closed": {"app": "blocked"}, "dns": {"example.com": "blocked"}, "uid": 65534, "cap_eff": "0000000000000000",
            "no_new_privs": "1", "seccomp": "2", "rootfs": "read-only", "memory_max": "2147483648", "pids_max": "512", "cpu_max": "200000 100000",
            "pid1_env_names": ["PATH"], "run_secrets_exists": False}
    def test_good(self): self.assertEqual(GL.evaluate_probe(self.GOOD, GL.expected_limits("browser")), [])
    def test_each_violation(self):
        for change, want in (({"closed": {"app": "OPEN"}}, "app"), ({"open": {"proxy": "blocked"}}, "proxy"), ({"dns": {"example.com": "RESOLVED"}}, "example.com"),
                             ({"uid": 0}, "uid_root_or_unknown"), ({"cap_eff": "00000000a80425fb"}, "capabilities"), ({"no_new_privs": "0"}, "no_new_privs"),
                             ({"seccomp": "0"}, "seccomp"), ({"rootfs": "WRITABLE"}, "rootfs_writable"), ({"memory_max": "max"}, "memory_max"),
                             ({"pid1_env_names": ["VULTR_INFERENCE_API_KEY"]}, "provider_key_material"), ({"run_secrets_exists": True}, "provider_key_material")):
            self.assertIn(want, GL.evaluate_probe({**self.GOOD, **change}, GL.expected_limits("browser")), change)
    def test_limits_match_hardening(self):
        self.assertEqual(GL.expected_limits("worker"), {"memory_max": 1536 * 1024 ** 2, "pids_max": 256, "cpu_max": "200000 100000"})

class TestTargets(unittest.TestCase):
    def job(self, with_gateway=True):
        j = GL.LLMEpisodeJob.__new__(GL.LLMEpisodeJob)
        j.with_gateway, j.ips, j.public = with_gateway, {"app": "10.1.0.2", "db": "10.1.0.3"}, {"name": "app", "port": 3000}
        j.proxy, j.proxy_ip, j.cdp_ip, j.gw_ip = "p", "10.1.0.4", "10.1.1.2", "10.233.0.1"
        j.nets, j.nets2 = {"app": "n-app", "agent": "n-agent"}, {"cdp": "n-cdp", "gw": "n-gw"}
        return j
    def targets(self, pos, **kw):
        with mock.patch.object(GL.D, "ip_of", return_value="10.1.2.2"), mock.patch.object(GL.D, "sh", return_value=mock.Mock(stdout="10.1.9.1\n")):
            return self.job(**kw).isolation_targets(pos)
    def test_worker_may_reach_only_cdp_and_gateway(self):
        t = self.targets("worker")
        self.assertEqual(t["open"], {"browser-cdp": ["10.1.1.2", 9222], "inference-gateway": ["10.233.0.1", C.GATEWAY_PORT]})
        for k in ("svc-app:3000", "svc-db:5432", "proxy-agent-side:8080", "proxy-app-side:8080", "proxy-alias:8080", "metadata-169.254.169.254:80", "internet-1.1.1.1:443",
                  "internet-v6-2606:4700:4700::1111:443", "rootless-host-10.0.2.2:22", "gateway-other-port:10.233.0.1:22", "cdp-net-gateway:22", "gw-net-gateway:8080"):
            self.assertIn(k, t["closed"], k)
        self.assertIn(C.INFERENCE_HOST, t["dns_closed"]); self.assertEqual(t["limits"], GL.expected_limits("worker"))
    def test_browser_may_reach_only_the_proxy(self):
        t = self.targets("browser")
        self.assertEqual(list(t["open"]), ["proxy-alias:8080"])
        for k in ("svc-app:3000", "inference-gateway", "proxy-app-side:8080", "agent-net-gateway:22", "cdp-net-gateway:9222", "metadata-169.254.169.254:80"): self.assertIn(k, t["closed"], k)
    def test_model_run_needs_gateway_and_token(self):
        j = self.job(with_gateway=False); j.gw_url = None
        with self.assertRaisesRegex(RuntimeError, "needs the gateway and a job token"): j.run_worker("http://afd-proxy:8080/", token="t")
        j = self.job(); j.gw_url = "http://10.233.0.1:8080"
        with self.assertRaisesRegex(RuntimeError, "needs the gateway and a job token"): j.run_worker("http://afd-proxy:8080/", token=None)

class TestConfigAndPaid(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp(prefix="afd-llmcfg-"); self.addCleanup(shutil.rmtree, self.d, True)
    def test_proposal_config_is_valid(self):
        cfg, sha = LE.load_config(ROOT / "studies" / "p3" / "gitea-ui-llm.json")
        self.assertEqual((cfg["episode_cap_usd"], cfg["total_cap_usd"], len(cfg["episodes"]), cfg["condition"]), (0.05, 0.10, 2, "lost_ack")); self.assertEqual(len(sha), 64)
    def test_limits_refused(self):
        for over, msg in (({"episode_cap_usd": 0.06}, "episode_cap_usd"), ({"total_cap_usd": 0.11}, "total_cap_usd"), ({"episodes": [{"policy": "none"}] * 3}, "episodes"),
                          ({"condition": "late_backend_commit"}, "condition"), ({"model": "glm-5.3"}, "model"), ({"episodes": [{"policy": "make_it_up"}]}, "policy"),
                          ({"max_steps": 40}, "max_steps")):
            with self.assertRaisesRegex(LE.Refused, msg, msg=over): LE.load_config(write_cfg(self.d, **over))
    def test_paid_requires_exact_confirmation(self):
        for confirm in (None, 0.05, 0.2):
            with self.assertRaisesRegex(LE.Refused, "confirm-total-usd"): LE.run(ROOT / "studies" / "p3" / "gitea-ui-llm.json", paid=True, confirm_total_usd=confirm)

class TestEpisodeLoop(unittest.TestCase):
    def run_loop(self, episodes):
        tmp = Path(tempfile.mkdtemp(prefix="afd-llmloop-")); calls = []
        self.addCleanup(shutil.rmtree, tmp, True)
        def ep(cfg, contract, e, i, work):
            calls.append(i); return episodes[i - 1], []
        with mock.patch.object(LE, "OUT", tmp), mock.patch.object(LE.W, "require_approval", return_value=({}, {"images": [], "contract_sha256": "c" * 64})), \
             mock.patch.object(LE.W, "require_local_images"), mock.patch.object(LE.W, "PROBES", tmp), mock.patch.object(LE.G, "up"), \
             mock.patch.object(LE, "preflight", return_value=({"P1": {"pass": True}}, True, [])), mock.patch.object(LE, "episode", side_effect=ep), mock.patch("builtins.print"):
            (tmp / ("c" * 64 + ".json")).write_text("{}")
            r = LE.run(ROOT / "studies" / "p3" / "gitea-ui-llm.json", paid=True, confirm_total_usd=0.10)
        return r, calls
    def rec(self, cost, cat="agent"): return {"category": cat, "spend": {"cost_usd": cost}, "isolation_gate": {"pass": cat != "isolation_gate_failed"}}
    def test_runs_both_within_caps(self):
        r, calls = self.run_loop([self.rec(0.01), self.rec(0.02)]); self.assertEqual(calls, [1, 2]); self.assertTrue(r["verifies"]); self.assertEqual(r["spent_total_usd"], 0.03)
    def test_stops_after_gate_failure(self):
        r, calls = self.run_loop([self.rec(0.0, "isolation_gate_failed"), self.rec(0.01)]); self.assertEqual(calls, [1])
    def test_refuses_episode_that_could_exceed_total(self):
        r, calls = self.run_loop([self.rec(0.06), self.rec(0.01)]); self.assertEqual(calls, [1])
        self.assertEqual(r["episodes"][1]["category"], "refused_total_cap")

if __name__ == "__main__":
    unittest.main()
