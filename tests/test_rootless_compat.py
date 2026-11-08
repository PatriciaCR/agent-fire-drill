"""Rootless (Vultr VM) compatibility of the generalized path (no Docker, no iptables: all host effects are mocked).
  1. `afd contract probe` / `afd qualify` never run the rootful host firewall (sudo iptables) when AFD_MODE=rootless;
     GenericJob then applies the in-namespace rules exactly like the refill path (afd.jobs.Job): F.ensure_netns().
     Rootful behaviour is unchanged.
  2. The probe topology (LLMEpisodeJob, with_gateway=False) runs in rootless mode; with a gateway (P3) it mirrors
     afd.jobs.Job: the host gateway service, no network connect, only the worker's gw network non-internal.
Run: python3 -m unittest tests.test_rootless_compat"""
import importlib.machinery, importlib.util, json, sys, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from afd import config as C, firewall as F, generic as GEN, generic_llm as GL, jobs as J, workflow as W  # noqa: E402

def load_cli():
    loader = importlib.machinery.SourceFileLoader("afd_cli_rootless", str(ROOT / "bin" / "afd"))
    spec = importlib.util.spec_from_loader("afd_cli_rootless", loader); m = importlib.util.module_from_spec(spec); loader.exec_module(m); return m

CONTRACT = json.loads((ROOT / "studies" / "m5" / "fixtures" / "latecommit-v1.3.json").read_text())

class TestCliFirewall(unittest.TestCase):
    def run_cli(self, rootless, args):
        cli = load_cli()
        with mock.patch.object(C, "ROOTLESS", rootless), mock.patch.object(F, "install") as inst, \
             mock.patch.object(W, "probe", side_effect=W.Refused("stop")), mock.patch.object(W, "qualify", side_effect=W.Refused("stop")), \
             mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            rc = cli.main(args)
        return rc, inst.call_count
    def test_rootless_never_runs_host_firewall(self):
        for args in (["contract", "probe", "c.json"], ["qualify", "--contract", "c.json"], ["qualify", "--contract", "c.json", "--agent-spec", "a.json"]):
            self.assertEqual(self.run_cli(True, args), (1, 0), args)
    def test_rootful_unchanged(self):
        for args in (["contract", "probe", "c.json"], ["qualify", "--contract", "c.json"]):
            self.assertEqual(self.run_cli(False, args), (1, 1), args)

class Stop(Exception): pass

class TestGenericJobNamespaceRules(unittest.TestCase):
    def enter(self, rootless):
        job = GEN.GenericJob(CONTRACT, "t")
        with mock.patch.object(C, "ROOTLESS", rootless), mock.patch.object(F, "ensure_netns") as ns, \
             mock.patch.object(J, "_active", return_value={}), mock.patch.object(J, "_set_active"), mock.patch.object(J, "_subnets", return_value=["10.0.0.0/28", "10.0.0.16/28", "10.0.0.32/28"]), \
             mock.patch.object(GEN.D, "create_internal_network", side_effect=Stop), mock.patch.object(GEN.GenericJob, "__exit__", return_value=False):
            with self.assertRaises(Stop): job.__enter__()
        return ns.call_count
    def test_rootless_applies_in_namespace_rules_before_networks(self):
        self.assertEqual(self.enter(True), 1)
    def test_rootful_does_not(self):
        self.assertEqual(self.enter(False), 0)

class TestProbeTopologyRootless(unittest.TestCase):
    def enter(self, rootless, with_gateway):
        job = GL.LLMEpisodeJob(CONTRACT, "t", with_gateway=with_gateway)
        def base_enter(self_):
            self_.nets = {"app": "n-app", "agent": "n-agent"}; return self_
        with mock.patch.object(C, "ROOTLESS", rootless), mock.patch.object(GEN.GenericJob, "__enter__", base_enter), \
             mock.patch.object(GL.LLMEpisodeJob, "__exit__", return_value=False), mock.patch.object(J, "_subnets", return_value=["10.0.0.48/28", "10.0.0.64/28", "10.0.0.80/28"]), \
             mock.patch.object(GL.D, "create_internal_network"), mock.patch.object(GL.D, "sh") as sh, mock.patch.object(GL.D, "ip_of", return_value="10.0.0.50"), \
             mock.patch.object(GL.J, "wait_cdp"):
            job.__enter__()
        return job, sh
    def test_harness_only_session_runs_rootless(self):
        job, sh = self.enter(True, False)
        self.assertIsNone(job.gw_url); self.assertEqual(job.cdp_ip, "10.0.0.50")
        self.assertFalse(any("connect" in c.args and C.GATEWAY_NAME in c.args for c in sh.call_args_list))   # no gateway attachment
    def test_rootless_gateway_mirrors_jobs_job(self):
        """P3: like afd.jobs.Job: the host gateway service address, no `docker network connect`, and ONLY the gw network
        non-internal (its egress is the slirp4netns socket the host firewall limits to the gateway address:port)."""
        with mock.patch.object(GL.D, "create_internal_network") as cin:
            job = GL.LLMEpisodeJob(CONTRACT, "t", with_gateway=True)
            def base_enter(self_): self_.nets = {"app": "n-app", "agent": "n-agent"}; return self_
            with mock.patch.object(C, "ROOTLESS", True), mock.patch.object(GEN.GenericJob, "__enter__", base_enter), \
                 mock.patch.object(GL.LLMEpisodeJob, "__exit__", return_value=False), mock.patch.object(J, "_subnets", return_value=["10.0.0.48/28", "10.0.0.64/28", "10.0.0.80/28"]), \
                 mock.patch.object(GL.D, "sh") as sh, mock.patch.object(GL.D, "ip_of", return_value="10.0.0.50"), mock.patch.object(GL.J, "wait_cdp"):
                job.__enter__()
        self.assertEqual((job.gw_ip, job.gw_url), (C.GATEWAY_ADDR, GL.G.url()))
        self.assertFalse(any("connect" in c.args and C.GATEWAY_NAME in c.args for c in sh.call_args_list))
        internal = {c.args[0].rsplit("-", 1)[1]: c.kwargs.get("internal", True) for c in cin.call_args_list}
        self.assertEqual(internal, {"cdp": True, "gw": False})
        browser_run = next(c for c in sh.call_args_list if c.args[:3] == ("docker", "run", "-d"))
        self.assertIn("n-agent", browser_run.args); self.assertNotIn(job.nets2["gw"], " ".join(map(str, sh.call_args_list)))   # the browser never joins gw
    def test_rootless_without_gateway_keeps_every_network_internal(self):
        with mock.patch.object(GL.D, "create_internal_network") as cin:
            self.enter_with(cin)
        self.assertTrue(all(c.kwargs.get("internal", True) for c in cin.call_args_list))
    def enter_with(self, cin):
        job = GL.LLMEpisodeJob(CONTRACT, "t", with_gateway=False)
        def base_enter(self_): self_.nets = {"app": "n-app", "agent": "n-agent"}; return self_
        with mock.patch.object(C, "ROOTLESS", True), mock.patch.object(GEN.GenericJob, "__enter__", base_enter), \
             mock.patch.object(GL.LLMEpisodeJob, "__exit__", return_value=False), mock.patch.object(J, "_subnets", return_value=["10.0.0.48/28", "10.0.0.64/28", "10.0.0.80/28"]), \
             mock.patch.object(GL.D, "sh"), mock.patch.object(GL.D, "ip_of", return_value="10.0.0.50"), mock.patch.object(GL.J, "wait_cdp"):
            job.__enter__()
        return job
    def test_rootful_gateway_attachment_unchanged(self):
        job, sh = self.enter(False, True)
        self.assertTrue(any(c.args[:3] == ("docker", "network", "connect") and C.GATEWAY_NAME in c.args for c in sh.call_args_list))
        self.assertTrue(job.gw_url.startswith("http://10.0.0.50:"))

if __name__ == "__main__":
    unittest.main()
