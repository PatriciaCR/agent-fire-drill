"""Job and run lifecycle. One job = one fresh site container + three two-member internal networks + one gateway token.
One run = one fresh browser container + one fresh worker container. Everything is labelled and removed in `finally`,
removal is verified, and `janitor()` removes leftovers of jobs that are no longer active."""
import base64, io, ipaddress, json, os, tarfile, time, uuid, urllib.request
from . import config as C, dockerops as D, firewall as F, gateway as G

MARK = "=====AFD-RESULT-BASE64====="
ACTIVE = C.VAR / "active_jobs.json"
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def http_json(url, payload=None, method="GET", headers=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with _NOPROXY.open(req, timeout=timeout) as r: return json.load(r)

def _active():
    try: return json.loads(ACTIVE.read_text())
    except Exception: return {}
def _set_active(d): C.VAR.mkdir(parents=True, exist_ok=True); ACTIVE.write_text(json.dumps(d))

def _subnets():
    """Three free /28s from the job pool (avoids any subnet already used by a Docker network)."""
    used = set()
    for n in D.sh("docker", "network", "ls", "-q").stdout.split():
        for cfg in (D.inspect_network(n).get("IPAM", {}).get("Config") or []):
            if cfg.get("Subnet"): used.add(ipaddress.ip_network(cfg["Subnet"], strict=False))
    free = [s for s in ipaddress.ip_network(C.JOB_POOL).subnets(new_prefix=28) if not any(s.overlaps(u) for u in used)]
    if len(free) < 3: raise RuntimeError("job subnet pool exhausted")
    return [str(s) for s in free[:3]]

def wait_cdp(ip, timeout=90):
    """Block until the browser's CDP endpoint answers (Chromium under resource limits can take >10 s to start)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try: http_json(f"http://{ip}:9222/json/version", timeout=2); return round(time.time() - t0, 1)
        except Exception: time.sleep(0.5)
    raise RuntimeError("browser CDP endpoint did not come up")

class Job:
    def __init__(self, budget_usd=None, label="job"):
        self.id = f"{label}-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.budget = budget_usd if budget_usd is not None else C.JOB_BUDGET_USD
        self.dir = C.JOBS_DIR / self.id; self.nets = {k: f"afdnet-{self.id}-{k}" for k in ("site", "cdp", "gw")}
        self.site = f"afd-{self.id}-site"; self.secret = uuid.uuid4().hex + uuid.uuid4().hex
        self.token = self.token_path = None; self.teardown_report = None

    def __enter__(self):
        self.dir.mkdir(parents=True, exist_ok=True); a = _active(); a[self.id] = {"pid": os.getpid(), "t": time.time()}; _set_active(a)
        try:
            if C.ROOTLESS: F.ensure_netns()   # in-namespace rules (defence in depth; the host uid rule is the hard boundary)
            for (k, name), subnet in zip(self.nets.items(), _subnets()):
                D.create_internal_network(name, subnet, self.id, internal=not (C.ROOTLESS and k == "gw"))
            D.sh("docker", "run", "-d", "--name", self.site, "--network", self.nets["site"], *D.labels(self.id, "site"),
                 *D.hardening("site", C.SITE_UID), "-e", f"BREAKINGWEB_CONTROLLER_SECRET={self.secret}", C.IMAGES["site"])
            if C.ROOTLESS:
                self.gw_ip, self.gw_url = C.GATEWAY_ADDR, G.url()
            else:
                D.sh("docker", "network", "connect", self.nets["gw"], C.GATEWAY_NAME)
                self.gw_ip = D.ip_of(C.GATEWAY_NAME, self.nets["gw"]); self.gw_url = f"http://{self.gw_ip}:8080"
            self.token, self.token_path = G.issue_token(self.id, self.budget)
            self.site_ip = D.ip_of(self.site, self.nets["site"])
            self.backend = f"http://{self.site_ip}:8080"
            for _ in range(60):
                try: http_json(self.backend + "/health", timeout=2); break
                except Exception: time.sleep(0.5)
            else: raise RuntimeError("site did not become healthy")
        except Exception:
            self.__exit__(None, None, None); raise
        return self

    def control(self, path, payload=None, method="GET"):
        return http_json(self.backend + path, payload, method, headers={"X-Drill-Secret": self.secret})

    def run_agent(self, run_id, launch_url, instruction, policy_text):
        """One episode: fresh browser + fresh worker. Returns (result dict, {name: bytes} screenshots, run facts)."""
        browser, worker = f"afd-{self.id}-{run_id}-browser", f"afd-{self.id}-{run_id}-worker"
        facts = {"browser": browser, "worker": worker}
        try:
            D.sh("docker", "run", "-d", "--name", browser, "--network", self.nets["site"], *D.labels(self.id, "browser"),
                 *D.hardening("browser", C.BROWSER_UID), C.IMAGES["browser"])
            D.sh("docker", "network", "connect", self.nets["cdp"], browser)
            cdp_ip = D.ip_of(browser, self.nets["cdp"])
            env = {"AFD_CDP_URL": f"http://{cdp_ip}:9222", "AFD_LAUNCH_URL": launch_url, "AFD_ALLOWED_HOST": self.site_ip,
                   "AFD_INSTRUCTION": instruction, "AFD_POLICY": policy_text or "", "AFD_GATEWAY_URL": f"{self.gw_url}/v1",
                   "AFD_JOB_TOKEN": self.token, "AFD_MODEL": C.MODEL, "AFD_MAX_STEPS": "25"}
            envargs = [x for k, v in env.items() for x in ("-e", f"{k}={v}")]
            D.sh("docker", "create", "--name", worker, "--network", self.nets["cdp"], *D.labels(self.id, "worker"),
                 *D.hardening("worker", C.WORKER_UID), *envargs, C.IMAGES["worker"])
            D.sh("docker", "network", "connect", self.nets["gw"], worker)
            facts["cdp_ready_s"] = wait_cdp(cdp_ip)
            t0 = time.time()
            try:
                r = D.sh("docker", "start", "-a", worker, check=False, timeout=C.RUN_WALLCLOCK_S)
                out, facts["worker_rc"] = r.stdout, r.returncode
            except Exception as e:
                out, facts["worker_rc"], facts["wallclock_killed"] = "", None, f"{type(e).__name__}"
            facts["seconds"] = round(time.time() - t0, 1)
            result, shots = {"error": "no result from worker"}, {}
            if MARK in out:
                with tarfile.open(fileobj=io.BytesIO(base64.b64decode(out.rsplit(MARK, 1)[1].strip())), mode="r") as t:
                    for m in t.getmembers():
                        data = t.extractfile(m).read()
                        if m.name == "result.json": result = json.loads(data)
                        elif m.name.startswith("screenshots/") and m.name.count("/") == 1 and m.name.endswith(".png"): shots[m.name] = data
            return result, shots, facts
        finally:
            facts["browser_removed"] = D.remove_container(browser); facts["worker_removed"] = D.remove_container(worker)

    def __exit__(self, *exc):
        rep = {"site_removed": D.remove_container(self.site)}
        if not C.ROOTLESS and D.exists("container", C.GATEWAY_NAME): D.sh("docker", "network", "disconnect", "-f", self.nets["gw"], C.GATEWAY_NAME, check=False)
        for k, n in self.nets.items():
            for c in D.labelled("container", f"afd.job={self.id}"): D.remove_container(c)
            rep[f"net_{k}_removed"] = D.remove_network(n)
        rep["token_revoked"] = G.revoke(self.token_path) if self.token_path else True
        rep["leftover_containers"] = D.labelled("container", f"afd.job={self.id}")
        rep["leftover_networks"] = D.labelled("network", f"afd.job={self.id}")
        a = _active(); a.pop(self.id, None); _set_active(a)
        self.teardown_report = rep
        (self.dir / "teardown.json").write_text(json.dumps(rep, indent=1))
        return False

def janitor():
    """Remove every labelled job container/network whose job is not active (e.g. after an orchestrator crash),
    and revoke tokens of jobs that are not active."""
    def alive(pid):
        try: os.kill(int(pid), 0); return True
        except Exception: return False
    active = {j for j, v in _active().items() if isinstance(v, dict) and alive(v.get("pid"))}
    removed = {"containers": [], "networks": [], "tokens": []}
    for c in D.labelled("container", "afd.role"):
        job = D.inspect(c)["Config"]["Labels"].get("afd.job")
        if job and job not in active and D.remove_container(c): removed["containers"].append(c)
    for n in D.labelled("network", "afd.role=net"):
        job = D.inspect_network(n)["Labels"].get("afd.job")
        if job and job not in active:
            if not C.ROOTLESS and D.exists("container", C.GATEWAY_NAME): D.sh("docker", "network", "disconnect", "-f", n, C.GATEWAY_NAME, check=False)
            if D.remove_network(n): removed["networks"].append(n)
    for p in (C.TOKEN_DIR.glob("*.json") if C.TOKEN_DIR.exists() else []):
        try: job = json.loads(p.read_text())["job_id"]
        except Exception: job = None
        if job not in active: os.remove(p); removed["tokens"].append(p.name[:12])
    _set_active({j: v for j, v in _active().items() if j in active})
    return removed
