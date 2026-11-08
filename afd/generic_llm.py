"""Real-LLM browser episodes through the generic contract-driven proxy (M2). Builds on afd.generic.GenericJob; adds two
internal networks and nothing else. Topology (all networks --internal; rootful dev mode):

  services --[app]-- fault proxy (alias afd-proxy) --[agent]-- browser --[cdp]-- worker --[gw]-- inference gateway
                                                                  \\--[cdp]-- login driver (harness, before the agent)

  - the browser is on the agent network only (+ cdp): it reaches the app solely as http://afd-proxy:8080;
  - the worker is on cdp + gw only: it cannot reach the app, the proxy or the internet; it holds only a per-episode
    gateway token (USD-capped by the gateway, revoked at the end); the provider key stays in the gateway;
  - the harness logs in through CDP before the worker starts, so credentials never reach the worker, prompt or evidence.
The worker is afd/agents/browser_worker_v2.py mounted read-only into the pinned worker image (not rebuilt).
No application-specific identifiers (tests/test_contract.py)."""
import json, socket, time
from pathlib import Path
from . import config as C, contract as K, dockerops as D, gateway as G, generic as GEN, jobs as J

ROOT = Path(__file__).resolve().parent.parent
WORKER_V2 = ROOT / "afd" / "agents" / "browser_worker_v2.py"
MARK = "=====AFD-RESULT-BASE64====="

PROBE = r'''
import json, os, socket, sys
def tcp(h, p):
    try: socket.create_connection((h, int(p)), 3).close(); return "OPEN"
    except Exception as e: return "blocked:" + type(e).__name__
t = json.loads(sys.argv[1])
print("PROBE:" + json.dumps({"tcp": {k: tcp(*v) for k, v in t.items()},
      "env_names": sorted(os.environ), "run_secrets_exists": os.path.exists("/run/secrets"), "uid": os.getuid()}))
'''

class LLMEpisodeJob(GEN.GenericJob):
    def __init__(self, contract, label="generic", with_gateway=True):
        super().__init__(contract, label); self.with_gateway = with_gateway   # False: $0 harness-only sessions (no gateway attached)

    def __enter__(self):
        super().__enter__()
        try:
            self.nets2 = {"cdp": f"afdnet-{self.id}-cdp", "gw": f"afdnet-{self.id}-gw"}
            for (k, name), subnet in zip(self.nets2.items(), J._subnets()): D.create_internal_network(name, subnet, self.id)
            # The LLM gateway attachment is not implemented for the rootless VM topology; harness-only sessions
            # (with_gateway=False, e.g. `afd contract probe`) need no gateway and run in both modes.
            if C.ROOTLESS and self.with_gateway: raise RuntimeError("LLM gateway attachment for generic jobs is not implemented in rootless mode (use with_gateway=False)")
            if self.with_gateway:
                D.sh("docker", "network", "connect", self.nets2["gw"], C.GATEWAY_NAME)
                self.gw_ip = D.ip_of(C.GATEWAY_NAME, self.nets2["gw"]); self.gw_url = f"http://{self.gw_ip}:{C.GATEWAY_PORT}"
            else: self.gw_ip = self.gw_url = None
            self.browser = f"afd-{self.id}-browser"
            D.sh("docker", "run", "-d", "--name", self.browser, "--network", self.nets["agent"], *D.labels(self.id, "browser"), *D.hardening("browser", C.BROWSER_UID), C.IMAGES["browser"])
            D.sh("docker", "network", "connect", self.nets2["cdp"], self.browser)
            self.cdp_ip = D.ip_of(self.browser, self.nets2["cdp"]); J.wait_cdp(self.cdp_ip)
        except Exception:
            self.__exit__(None, None, None); raise
        return self

    def harness_container(self, nets, args, label, mounts=(), env=None, timeout=300):
        """A short-lived container from the worker image (non-root, read-only, no capabilities) on the given networks."""
        name = f"afd-{self.id}-{label}-{int(time.time() * 1000) % 10**8}"
        try:
            D.sh("docker", "create", "--name", name, "--network", nets[0], *D.labels(self.id, label), *D.hardening("worker", C.WORKER_UID),
                 *[x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")], *[x for m in mounts for x in ("-v", m)],
                 "--entrypoint", "python", C.IMAGES["worker"], *args)
            for n in nets[1:]: D.sh("docker", "network", "connect", n, name)
            r = D.sh("docker", "start", "-a", name, check=False, timeout=timeout)
            return r
        finally:
            D.remove_container(name)

    def probe(self, nets, targets):
        r = self.harness_container(nets, ["-I", "-c", PROBE, json.dumps(targets)], "probe", timeout=60)
        return json.loads(r.stdout.split("PROBE:", 1)[1]) if "PROBE:" in r.stdout else {"error": r.stderr[-400:]}

    def drive(self, driver_src, steps):
        """Run CDP driver steps against the browser from the cdp network (harness only; e.g. the login bootstrap)."""
        r = self.harness_container([self.nets2["cdp"]], ["-c", driver_src, f"{self.cdp_ip}:9222", json.dumps(steps)], "driver")
        return json.loads(r.stdout.split("RESULT:", 1)[1]) if "RESULT:" in r.stdout else [{"error": r.stderr[-600:]}]

    def run_worker(self, launch_url, instruction="", policy="", token=None, max_steps=15, evidence_only=False, timeout=900):
        env = {"AFD_CDP_URL": f"http://{self.cdp_ip}:9222", "AFD_LAUNCH_URL": launch_url, "AFD_ALLOWED_HOST": GEN.PROXY_ALIAS,
               "AFD_INSTRUCTION": instruction, "AFD_POLICY": policy, "AFD_MAX_STEPS": str(max_steps)}
        if evidence_only: env["AFD_EVIDENCE_ONLY"] = "1"
        else: env.update({"AFD_GATEWAY_URL": f"{self.gw_url}/v1", "AFD_JOB_TOKEN": token, "AFD_MODEL": C.MODEL})
        nets = [self.nets2["cdp"]] + ([] if evidence_only else [self.nets2["gw"]])
        t0 = time.time()
        try: r = self.harness_container(nets, ["/afd/browser_worker_v2.py"], "worker", mounts=[f"{WORKER_V2}:/afd/browser_worker_v2.py:ro"], env=env, timeout=timeout)
        except Exception as e: return {"error": f"worker: {type(e).__name__}"}, {}, round(time.time() - t0, 1)
        import base64, io, tarfile
        result, shots = {"error": "no result from worker", "stderr_tail": (r.stderr or "")[-400:]}, {}
        if MARK in r.stdout:
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(r.stdout.rsplit(MARK, 1)[1].strip())), mode="r") as t:
                for m in t.getmembers():
                    data = t.extractfile(m).read()
                    if m.name == "result.json": result = json.loads(data)
                    elif m.name.startswith("screenshots/") and m.name.count("/") == 1 and m.name.endswith(".png"): shots[m.name] = data
        return result, shots, round(time.time() - t0, 1)

    def __exit__(self, *exc):
        rep_extra = {}
        if getattr(self, "browser", None): rep_extra["browser_removed"] = D.remove_container(self.browser)
        if getattr(self, "nets2", None) and not C.ROOTLESS and D.exists("container", C.GATEWAY_NAME):
            D.sh("docker", "network", "disconnect", "-f", self.nets2["gw"], C.GATEWAY_NAME, check=False)
        for c in D.labelled("container", f"afd.job={self.id}"): D.remove_container(c)
        for k, n in (getattr(self, "nets2", None) or {}).items(): rep_extra[f"net_{k}_removed"] = D.remove_network(n)
        super().__exit__(*exc)
        self.teardown_report.update(rep_extra)
        self.teardown_report["leftover_containers"] = D.labelled("container", f"afd.job={self.id}")
        self.teardown_report["leftover_networks"] = D.labelled("network", f"afd.job={self.id}")
        return False
