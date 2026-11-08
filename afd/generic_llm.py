"""Real-LLM browser episodes through the generic contract-driven proxy (M2). Builds on afd.generic.GenericJob; adds two
internal networks and nothing else. Topology (all networks --internal; rootful dev mode):

  services --[app]-- fault proxy (alias afd-proxy) --[agent]-- browser --[cdp]-- worker --[gw]-- inference gateway
                                                                  \\--[cdp]-- login driver (harness, before the agent)

Rootless (VM): the gateway attachment mirrors afd.jobs.Job exactly. The gateway is the host service at
C.GATEWAY_ADDR:C.GATEWAY_PORT; the gw network is the ONLY non-internal network, and only the worker joins it. Its
traffic leaves the rootless namespace only through slirp4netns as afd-orch, which the root-owned host firewall limits to
the gateway address and port; F.ensure_netns() (GenericJob.__enter__) adds the same rule inside the namespace.
ENFORCED, not assumed: every worker run starts with ISOLATION_GATE inside the actual worker container (same image,
networks, env and hardening). It must find exactly the allowed endpoints open and everything else closed, and verify the
container restrictions, BEFORE the unchanged worker code is executed; otherwise the container exits and no model call
is possible. browser_isolation() checks the actual browser container from inside (docker exec) in the same way.

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

GATE_EXIT = 42
# Runs as the first thing inside the actual worker container. argv[1] = JSON {"open": {name: [host, port]}, "closed": {...},
# "dns_closed": [names], "limits": {...}, "then": path or null}. Pass: os.execv() the worker (same interpreter, same env);
# fail: print the evidence and exit GATE_EXIT before any worker code runs.
ISOLATION_GATE = r'''
import json, os, socket, sys
t = json.loads(sys.argv[1])
def tcp(h, p):
    try:
        fam = socket.AF_INET6 if ":" in h else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(3); s.connect((h, int(p))); s.close(); return "OPEN"
    except Exception as e: return "blocked:" + type(e).__name__
def dns(n):
    try: socket.getaddrinfo(n, 443); return "RESOLVED"
    except Exception as e: return "blocked:" + type(e).__name__
def read(p):
    try: return open(p).read().strip()
    except Exception as e: return "unreadable:" + type(e).__name__
st = dict(l.split(":", 1) for l in read("/proc/self/status").splitlines() if ":" in l)
try: open("/afd-gate-write-test", "w").close(); rootfs = "WRITABLE"
except OSError as e: rootfs = "read-only:" + str(e.errno)
r = {"open": {k: tcp(*v) for k, v in t["open"].items()}, "closed": {k: tcp(*v) for k, v in t["closed"].items()},
     "dns": {n: dns(n) for n in t.get("dns_closed", [])}, "uid": os.getuid(), "cap_eff": st.get("CapEff", "").strip(),
     "no_new_privs": st.get("NoNewPrivs", "").strip(), "seccomp": st.get("Seccomp", "").strip(), "rootfs": rootfs,
     "memory_max": read("/sys/fs/cgroup/memory.max"), "pids_max": read("/sys/fs/cgroup/pids.max"), "cpu_max": read("/sys/fs/cgroup/cpu.max"),
     "env_names": sorted(os.environ), "run_secrets_exists": os.path.exists("/run/secrets")}
lim = t.get("limits") or {}
bad = [k for k, v in r["open"].items() if v != "OPEN"] + [k for k, v in r["closed"].items() if v == "OPEN"] + [n for n, v in r["dns"].items() if v == "RESOLVED"]
if r["uid"] == 0: bad.append("uid_root")
if r["cap_eff"].strip("0"): bad.append("capabilities")
if r["no_new_privs"] != "1": bad.append("no_new_privs")
if r["seccomp"] != "2": bad.append("seccomp")
if not rootfs.startswith("read-only"): bad.append("rootfs_writable")
for k in ("memory_max", "pids_max", "cpu_max"):
    if k in lim and r[k] != str(lim[k]): bad.append(k)
if any("VULTR" in n.upper() or n.upper().endswith("API_KEY") for n in r["env_names"]) or r["run_secrets_exists"]: bad.append("provider_key_material")
r["violations"] = bad; r["pass"] = not bad
print("AFD-GATE:" + json.dumps(r), flush=True)
if bad: sys.exit(42)
if t.get("then"): os.execv(sys.executable, [sys.executable, t["then"]])
'''

# Runs inside the actual browser container (docker exec; the image has perl but no python). argv: JSON as above.
BROWSER_PROBE = r'''
use strict; use IO::Socket::IP; use Socket;
my $t = $ARGV[0]; my (%o, %c, %d);
sub tcp { my ($h, $p) = @_; my $s = IO::Socket::IP->new(PeerHost => $h, PeerPort => $p, Proto => "tcp", Timeout => 3); if ($s) { close $s; return "OPEN" } return "blocked" }
while ($t =~ /"(open|closed)":\{([^}]*)\}/g) { my ($kind, $body) = ($1, $2);
  while ($body =~ /"([^"]+)":\["([^"]+)",(\d+)\]/g) { my $v = tcp($2, $3); if ($kind eq "open") { $o{$1} = $v } else { $c{$1} = $v } } }
if ($t =~ /"dns_closed":\[([^\]]*)\]/) { for my $n ($1 =~ /"([^"]+)"/g) { my ($e) = Socket::getaddrinfo($n, "443"); $d{$n} = $e ? "blocked" : "RESOLVED" } }
my %st = map { /^(\w+):\s*(.*)$/ ? ($1, $2) : () } do { open(my $f, "<", "/proc/self/status"); <$f> };
my $rw = open(my $w, ">", "/afd-probe-write-test") ? "WRITABLE" : "read-only";
sub rd { open(my $f, "<", $_[0]) or return "unreadable"; my $x = <$f>; chomp $x; $x }
my @env = do { open(my $f, "<", "/proc/1/environ"); local $/; my $x = <$f> // ""; map { (split /=/)[0] } grep { length } split /\0/, $x };
my $j = sub { my %h = @_; "{" . join(",", map { "\"$_\":\"$h{$_}\"" } sort keys %h) . "}" };
print "AFD-BPROBE:{\"open\":" . $j->(%o) . ",\"closed\":" . $j->(%c) . ",\"dns\":" . $j->(%d) . ",\"uid\":" . $< . ",\"cap_eff\":\"$st{CapEff}\",\"no_new_privs\":\"$st{NoNewPrivs}\",\"seccomp\":\"$st{Seccomp}\",\"rootfs\":\"$rw\",\"memory_max\":\"" . rd("/sys/fs/cgroup/memory.max") . "\",\"pids_max\":\"" . rd("/sys/fs/cgroup/pids.max") . "\",\"cpu_max\":\"" . rd("/sys/fs/cgroup/cpu.max") . "\",\"pid1_env_names\":[" . join(",", map { "\"$_\"" } sort @env) . "],\"run_secrets_exists\":" . (-e "/run/secrets" ? "true" : "false") . "}\n";
'''

def expected_limits(kind):
    """cgroup v2 values Docker sets for D.hardening(kind) (memory, cpus, pids from C.LIMITS)."""
    mem, cpus, pids, _ = C.LIMITS[kind]
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    return {"memory_max": int(mem[:-1]) * units[mem[-1].lower()], "pids_max": pids, "cpu_max": f"{int(float(cpus) * 100000)} 100000"}

def evaluate_probe(r, limits):
    """Shared pass rule for the browser probe (the worker gate applies the same rule inside the container)."""
    bad = [k for k, v in r.get("open", {}).items() if v != "OPEN"] + [k for k, v in r.get("closed", {}).items() if v == "OPEN"] + [n for n, v in r.get("dns", {}).items() if v == "RESOLVED"]
    if r.get("uid") in (0, None): bad.append("uid_root_or_unknown")
    if str(r.get("cap_eff", "")).strip("0") or not r.get("cap_eff"): bad.append("capabilities")
    if r.get("no_new_privs") != "1": bad.append("no_new_privs")
    if r.get("seccomp") != "2": bad.append("seccomp")
    if not str(r.get("rootfs", "")).startswith("read-only"): bad.append("rootfs_writable")
    for k, v in (limits or {}).items():
        if str(r.get(k)) != str(v): bad.append(k)
    if any("VULTR" in n.upper() or n.upper().endswith("API_KEY") for n in r.get("env_names", []) + r.get("pid1_env_names", [])) or r.get("run_secrets_exists") is not False: bad.append("provider_key_material")
    return bad

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
            # Same as afd.jobs.Job: in rootless mode only the gw network is non-internal (and only with a gateway attached).
            for (k, name), subnet in zip(self.nets2.items(), J._subnets()):
                D.create_internal_network(name, subnet, self.id, internal=not (C.ROOTLESS and k == "gw" and self.with_gateway))
            if self.with_gateway and C.ROOTLESS:
                self.gw_ip, self.gw_url = C.GATEWAY_ADDR, G.url()
            elif self.with_gateway:
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

    def _net_gateways(self, keys):
        out = {}
        for k in keys:
            n = {**self.nets, **self.nets2}[k]
            gw = D.sh("docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Gateway}}{{end}}", n, check=False).stdout.strip()
            if gw:
                for port in (22, 80, 443, 2375, 8080, 9222): out[f"{k}-net-gateway:{port}"] = [gw, port]
        return out

    def isolation_targets(self, position):
        """What must be open and what must be closed from a position ('worker' or 'browser'). Closed always includes every
        app service, both sides of the proxy (worker) or the app (browser), the gateway (browser), other gateway ports,
        the networks' host-side gateways, the rootless host alias, cloud metadata, IPv4/IPv6 internet and public DNS."""
        closed = {f"svc-{n}:{p}": [ip, p] for n, ip in self.ips.items() for p in sorted({self.public["port"] if n == self.public["name"] else 0, 80, 5432, 3000, 22} - {0})}
        closed.update({"metadata-169.254.169.254:80": ["169.254.169.254", 80], "internet-1.1.1.1:443": ["1.1.1.1", 443], "internet-8.8.8.8:53": ["8.8.8.8", 53],
                       "internet-v6-2606:4700:4700::1111:443": ["2606:4700:4700::1111", 443], "rootless-host-10.0.2.2:22": ["10.0.2.2", 22],
                       "rootless-host-10.0.2.2:80": ["10.0.2.2", 80], "rootless-host-10.0.2.2:443": ["10.0.2.2", 443], "rootless-dns-10.0.2.3:53": ["10.0.2.3", 53]})
        if self.with_gateway: closed[f"gateway-other-port:{self.gw_ip}:22"] = [self.gw_ip, 22]
        closed.update({k: list(v) for k, v in (getattr(self, "extra_closed", None) or {}).items()})   # deployment-specific, e.g. the VM's public address
        dns = ["example.com", C.INFERENCE_HOST]
        proxy_agent_ip = D.ip_of(self.proxy, self.nets["agent"])
        if position == "worker":
            opn = {"browser-cdp": [self.cdp_ip, 9222], **({"inference-gateway": [self.gw_ip, C.GATEWAY_PORT]} if self.with_gateway else {})}
            closed.update({"proxy-agent-side:8080": [proxy_agent_ip, 8080], "proxy-app-side:8080": [self.proxy_ip, 8080], "proxy-alias:8080": [GEN.PROXY_ALIAS, 8080]})
            closed.update(self._net_gateways(["cdp"] + (["gw"] if self.with_gateway else [])))
            return {"open": opn, "closed": closed, "dns_closed": dns + [GEN.PROXY_ALIAS, self.public["name"]], "limits": expected_limits("worker")}
        closed.update({"proxy-app-side:8080": [self.proxy_ip, 8080]})
        if self.with_gateway: closed["inference-gateway"] = [self.gw_ip, C.GATEWAY_PORT]
        closed.update(self._net_gateways(["agent", "cdp"]))
        return {"open": {"proxy-alias:8080": [GEN.PROXY_ALIAS, 8080]}, "closed": closed, "dns_closed": dns + [self.public["name"]], "limits": expected_limits("browser")}

    def browser_isolation(self):
        """From INSIDE the actual browser container (docker exec, as its own user, inheriting its restrictions)."""
        t = self.isolation_targets("browser")
        r = D.sh("docker", "exec", self.browser, "perl", "-e", BROWSER_PROBE, json.dumps(t, separators=(",", ":")), check=False, timeout=180)
        got = json.loads(r.stdout.split("AFD-BPROBE:", 1)[1]) if "AFD-BPROBE:" in r.stdout else {"error": (r.stderr or "")[-400:]}
        inspect = D.inspect(self.browser)
        mounts = [m.get("Destination") for m in inspect.get("Mounts") or []]
        env_names = sorted(e.split("=", 1)[0] for e in inspect["Config"].get("Env") or [])
        got["inspect"] = {"mounts": mounts, "env_names": env_names, "networks": sorted((inspect.get("NetworkSettings") or {}).get("Networks", {}))}
        bad = evaluate_probe(got, t["limits"]) if "error" not in got else ["probe_failed"]
        if mounts: bad.append("unexpected_mounts")
        if any("VULTR" in n.upper() or n.upper().endswith("API_KEY") for n in env_names): bad.append("provider_key_in_env")
        return {"targets": t, "result": got, "violations": bad, "pass": not bad}

    def probe(self, nets, targets):
        r = self.harness_container(nets, ["-I", "-c", PROBE, json.dumps(targets)], "probe", timeout=60)
        return json.loads(r.stdout.split("PROBE:", 1)[1]) if "PROBE:" in r.stdout else {"error": r.stderr[-400:]}

    def drive(self, driver_src, steps):
        """Run CDP driver steps against the browser from the cdp network (harness only; e.g. the login bootstrap)."""
        r = self.harness_container([self.nets2["cdp"]], ["-c", driver_src, f"{self.cdp_ip}:9222", json.dumps(steps)], "driver")
        return json.loads(r.stdout.split("RESULT:", 1)[1]) if "RESULT:" in r.stdout else [{"error": r.stderr[-600:]}]

    def run_worker(self, launch_url, instruction="", policy="", token=None, max_steps=15, evidence_only=False, timeout=900, gate_only=False):
        """Every run starts with ISOLATION_GATE inside this same container; the worker code runs only if the gate passes.
        gate_only=True: the identical container (image, networks, env incl. the token, hardening) that stops after the gate."""
        env = {"AFD_CDP_URL": f"http://{self.cdp_ip}:9222", "AFD_LAUNCH_URL": launch_url, "AFD_ALLOWED_HOST": GEN.PROXY_ALIAS,
               "AFD_INSTRUCTION": instruction, "AFD_POLICY": policy, "AFD_MAX_STEPS": str(max_steps)}
        if evidence_only: env["AFD_EVIDENCE_ONLY"] = "1"
        else: env.update({"AFD_GATEWAY_URL": f"{self.gw_url}/v1", "AFD_JOB_TOKEN": token, "AFD_MODEL": C.MODEL})
        nets = [self.nets2["cdp"]] + ([] if evidence_only else [self.nets2["gw"]])
        if not evidence_only and not (self.with_gateway and self.gw_url and token): raise RuntimeError("a model run needs the gateway and a job token")
        targets = self.isolation_targets("worker")
        if evidence_only: targets["open"].pop("inference-gateway", None)
        else: targets["open"]["inference-gateway"] = [self.gw_ip, C.GATEWAY_PORT]
        if evidence_only and self.with_gateway: targets["closed"]["inference-gateway"] = [self.gw_ip, C.GATEWAY_PORT]
        targets["then"] = None if gate_only else "/afd/browser_worker_v2.py"
        t0 = time.time()
        try: r = self.harness_container(nets, ["-I", "-c", ISOLATION_GATE, json.dumps(targets)], "worker", mounts=[f"{WORKER_V2}:/afd/browser_worker_v2.py:ro"], env=env, timeout=timeout)
        except Exception as e: return {"error": f"worker: {type(e).__name__}"}, {}, round(time.time() - t0, 1)
        gate = json.loads(r.stdout.split("AFD-GATE:", 1)[1].splitlines()[0]) if "AFD-GATE:" in r.stdout else {"pass": False, "violations": ["gate_output_missing"], "stderr_tail": (r.stderr or "")[-400:]}
        gate["targets"] = targets; gate["exit_code"] = r.returncode
        if gate_only or not gate.get("pass"):
            return {"isolation_gate": gate, **({} if gate.get("pass") else {"error": "isolation gate failed: worker not started"})}, {}, round(time.time() - t0, 1)
        import base64, io, tarfile
        result, shots = {"error": "no result from worker", "stderr_tail": (r.stderr or "")[-400:]}, {}
        if MARK in r.stdout:
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(r.stdout.rsplit(MARK, 1)[1].strip())), mode="r") as t:
                for m in t.getmembers():
                    data = t.extractfile(m).read()
                    if m.name == "result.json": result = json.loads(data)
                    elif m.name.startswith("screenshots/") and m.name.count("/") == 1 and m.name.endswith(".png"): shots[m.name] = data
        result["isolation_gate"] = gate
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
