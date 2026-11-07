"""Containment acceptance suite (no inference cost). Probes run INSIDE containers launched with exactly the hardening and
networks of their role in a real job. The key comparison happens on the admin side (sudo reads the key file); probes never
receive the key. Usage: python3 tests/acceptance/containment.py [--compare-without-firewall]
Writes var/acceptance/containment.json; exit 0 only if every check passes."""
import base64, http.server, json, os, signal, socketserver, subprocess, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from afd import config as C, dockerops as D, firewall as F, gateway as G, jobs as J  # noqa: E402

HOST_PORT = 18080
RESULTS, OK = {}, [True]
def check(name, cond, detail=None):
    RESULTS[name] = {"pass": bool(cond), "detail": detail}; OK[0] &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {json.dumps(detail, default=str)[:220] if detail is not None else ''}", flush=True)

def host_listener():
    class Q(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"host-service")
        def log_message(self, *a): pass
    srv = socketserver.TCPServer(("0.0.0.0", HOST_PORT), Q); threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv

WORKER_PROBE = r'''
import json, os, socket, urllib.request, base64, io, tarfile
R = {}
def tcp(host, port, t=3):
    try: socket.create_connection((host, port), timeout=t).close(); return "OPEN"
    except Exception as e: return "blocked:" + type(e).__name__
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(url, headers=None, data=None):
    try:
        r = op.open(urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET"), timeout=5); return r.status
    except urllib.error.HTTPError as e: return e.code
    except Exception as e: return "blocked:" + type(e).__name__
R["docker_sock"] = [p for p in ("/var/run/docker.sock", "/run/docker.sock") if os.path.exists(p)]
R["secrets_dir"] = os.path.exists("/run/secrets")
R["internet_1.1.1.1:80"] = tcp("1.1.1.1", 80)
try: ip = socket.gethostbyname("api.vultrinference.com"); R["inference_direct"] = tcp(ip, 443)
except Exception as e: R["inference_direct"] = "blocked:dns:" + type(e).__name__
R["metadata"] = tcp("169.254.169.254", 80)
R["host_ports"] = {h: tcp(h, int(os.environ["HOST_PORT"])) for h in os.environ["HOST_BRIDGE_IPS"].split(",")}
R["site_direct"] = tcp(os.environ["SITE_IP"], 8080)
R["gateway_health"] = get(os.environ["GW"] + "/healthz")
R["gateway_no_token"] = get(os.environ["GW"] + "/v1/chat/completions", data=b"{}", headers={"Content-Type": "application/json"})
R["gateway_with_token_bad_model"] = get(os.environ["GW"] + "/v1/chat/completions", data=json.dumps({"model": "not-allowed", "messages": []}).encode(),
                                        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AFD_JOB_TOKEN"]})
R["cdp"] = get(os.environ["CDP"] + "/json/version")
st = open("/proc/self/status").read()
R["uid"] = os.getuid(); R["CapEff"] = [l.split()[1] for l in st.splitlines() if l.startswith("CapEff")][0]
R["NoNewPrivs"] = [l.split()[1] for l in st.splitlines() if l.startswith("NoNewPrivs")][0]
try: open("/etc/afd-test", "w"); R["root_write"] = "ALLOWED"
except OSError as e: R["root_write"] = "blocked:" + str(e.errno)
R["memory_max"] = open("/sys/fs/cgroup/memory.max").read().strip() if os.path.exists("/sys/fs/cgroup/memory.max") else None
R["pids_max"] = open("/sys/fs/cgroup/pids.max").read().strip() if os.path.exists("/sys/fs/cgroup/pids.max") else None
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as t:
    env = "\n".join(f"{k}={v}" for k, v in os.environ.items()).encode(); ti = tarfile.TarInfo("ENV"); ti.size = len(env); t.addfile(ti, io.BytesIO(env))
    for base in ("/run", "/etc", "/tmp", "/app", "/home", "/root", "/var"):
        for root, dirs, files in os.walk(base):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if os.path.isfile(p) and os.path.getsize(p) < 1_000_000: t.add(p, arcname=p.lstrip("/"))
                except Exception: pass
R["_files_b64"] = base64.b64encode(buf.getvalue()).decode()
print("PROBE:" + json.dumps(R))
'''

def run_probe_container(job, role, image, nets, cmd, env=None, uid=None, entry=None):
    name = f"afd-{job.id}-probe-{role}"
    envargs = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
    D.sh("docker", "create", "--name", name, "--network", nets[0], *D.labels(job.id, f"probe-{role}"), *D.hardening(role, uid), *envargs,
         *(["--entrypoint", entry] if entry else []), image, *cmd)
    for n in nets[1:]: D.sh("docker", "network", "connect", n, name)
    try:
        r = D.sh("docker", "start", "-a", name, check=False, timeout=180)
        return r, D.inspect(name)["State"]
    finally:
        D.remove_container(name)

def bridge_gateways(job):
    out = []
    for n in job.nets.values():
        cfg = (D.inspect_network(n).get("IPAM", {}).get("Config") or [{}])[0]
        out.append(cfg.get("Gateway") or cfg["Subnet"].split("/")[0].rsplit(".", 1)[0] + ".1")
    return out

def key_bytes():
    raw = D.sh("sudo", "-n", "cat", str(C.KEY_FILE)).stdout.strip()
    return raw.encode()

def main():
    compare = "--compare-without-firewall" in sys.argv
    fw = F.install(); check("firewall_installed", True, fw)
    G.up(); key = key_bytes()
    check("orchestrator_user_cannot_read_key_file", not os.access(C.KEY_FILE, os.R_OK), {"key_file_mode": oct(os.stat(C.KEY_FILE).st_mode & 0o777) if os.access(C.SECRET_DIR, os.X_OK) else "dir not traversable"})
    srv = host_listener()
    with J.Job(budget_usd=0.01, label="containment") as job:
        hosts = bridge_gateways(job)
        # ---- worker probe (worker image, worker hardening, worker networks: cdp + gw) ----
        browser = f"afd-{job.id}-pb-browser"
        D.sh("docker", "run", "-d", "--name", browser, "--network", job.nets["site"], *D.labels(job.id, "browser"), *D.hardening("browser", C.BROWSER_UID), C.IMAGES["browser"])
        D.sh("docker", "network", "connect", job.nets["cdp"], browser); cdp_ip = D.ip_of(browser, job.nets["cdp"])
        check("browser_cdp_ready", True, {"seconds": J.wait_cdp(cdp_ip)})
        env = {"HOST_PORT": HOST_PORT, "HOST_BRIDGE_IPS": ",".join(hosts), "SITE_IP": job.site_ip, "GW": f"http://{job.gw_ip}:8080",
               "CDP": f"http://{cdp_ip}:9222", "AFD_JOB_TOKEN": job.token}
        r, state = run_probe_container(job, "worker", C.IMAGES["worker"], [job.nets["cdp"], job.nets["gw"]], ["python", "-c", WORKER_PROBE], env, C.WORKER_UID)
        P = json.loads(r.stdout.split("PROBE:", 1)[1]) if "PROBE:" in r.stdout else {}
        blob = base64.b64decode(P.pop("_files_b64", "")) if P else b""
        check("worker_key_absent_env_and_files", bool(P) and key not in blob, {"scanned_bytes": len(blob)})
        check("worker_no_docker_socket", P.get("docker_sock") == [] and P.get("secrets_dir") is False, {k: P.get(k) for k in ("docker_sock", "secrets_dir")})
        check("worker_no_internet", str(P.get("internet_1.1.1.1:80")).startswith("blocked"), P.get("internet_1.1.1.1:80"))
        check("worker_no_direct_inference", str(P.get("inference_direct")).startswith("blocked"), P.get("inference_direct"))
        check("worker_no_metadata", str(P.get("metadata")).startswith("blocked"), P.get("metadata"))
        check("worker_no_host_ports", all(str(v).startswith("blocked") for v in (P.get("host_ports") or {"x": "OPEN"}).values()), P.get("host_ports"))
        check("worker_cannot_reach_site_directly", str(P.get("site_direct")).startswith("blocked"), P.get("site_direct"))
        check("worker_reaches_gateway_and_cdp_only_as_allowed", P.get("gateway_health") == 200 and P.get("gateway_no_token") == 401 and P.get("gateway_with_token_bad_model") == 403 and P.get("cdp") == 200,
              {k: P.get(k) for k in ("gateway_health", "gateway_no_token", "gateway_with_token_bad_model", "cdp")})
        check("worker_identity_and_limits", P.get("uid") == C.WORKER_UID and P.get("CapEff") == "0000000000000000" and P.get("NoNewPrivs") == "1"
              and str(P.get("root_write")).startswith("blocked") and P.get("memory_max") not in (None, "max") and P.get("pids_max") not in (None, "max"),
              {k: P.get(k) for k in ("uid", "CapEff", "NoNewPrivs", "root_write", "memory_max", "pids_max")})
        # ---- worker resource-limit enforcement ----
        r, st = run_probe_container(job, "worker", C.IMAGES["worker"], [job.nets["cdp"]], ["python", "-c", "b = bytearray(3 * 1024**3)"], {}, C.WORKER_UID)
        check("worker_memory_limit_enforced", st.get("OOMKilled") is True or st.get("ExitCode") == 137, {"OOMKilled": st.get("OOMKilled"), "exit": st.get("ExitCode")})
        fork = "import os, time\nn=0\ntry:\n    for _ in range(3000):\n        if os.fork()==0: time.sleep(5); os._exit(0)\n        n+=1\n    print('FORKS', n)\nexcept OSError: print('FORKS', n)"
        r, st = run_probe_container(job, "worker", C.IMAGES["worker"], [job.nets["cdp"]], ["python", "-c", fork], {}, C.WORKER_UID)
        nforks = int(r.stdout.split("FORKS")[-1].strip() or 0) if "FORKS" in r.stdout else None
        check("worker_pids_limit_enforced", nforks is not None and nforks < C.LIMITS["worker"][2], {"forks_before_limit": nforks, "pids_limit": C.LIMITS["worker"][2]})
        # ---- browser probe (browser image, browser networks: site + cdp) ----
        bprobe = ("for t in 1.1.1.1:80 169.254.169.254:80 " + " ".join(f"{h}:{HOST_PORT}" for h in hosts) + f" {job.gw_ip}:8080 {job.site_ip}:8080; do "
                  "if socat -u OPEN:/dev/null TCP:$t,connect-timeout=3 2>/dev/null; then echo \"$t OPEN\"; else echo \"$t blocked\"; fi; done; "
                  "id -u; grep CapEff /proc/self/status | cut -f2; env | cut -d= -f1 | sort | tr '\\n' ' '")
        r, _ = run_probe_container(job, "browser", C.IMAGES["browser"], [job.nets["site"], job.nets["cdp"]], ["-c", bprobe], {}, C.BROWSER_UID, entry="sh")
        lines = r.stdout.strip().splitlines()
        res = dict(l.split(" ", 1) for l in lines if l.count(":") == 1 and " " in l and l.split(" ")[0].count(".") == 3)
        check("browser_reaches_site_only", res.get(f"{job.site_ip}:8080") == "OPEN" and all(v == "blocked" for k, v in res.items() if k != f"{job.site_ip}:8080"), res)
        check("browser_identity_and_env", lines[-3:-2] == [str(C.BROWSER_UID)] and lines[-2] == "0000000000000000" and not any(s in lines[-1] for s in ("KEY", "TOKEN", "SECRET")),
              {"uid": lines[-3:-2], "CapEff": lines[-2:-1], "env": lines[-1][:200]})
        D.remove_container(browser)
        # ---- gateway egress (exec in the gateway) ----
        gprobe = ("import socket\nfor h,p in [('" + fw["inference_ips"][0] + "',443),('1.1.1.1',443),('169.254.169.254',80),('8.8.8.8',53)]:\n"
                  "    try: socket.create_connection((h,p),timeout=4).close(); print(h,p,'OPEN')\n    except Exception as e: print(h,p,'blocked',type(e).__name__)")
        g = D.sh("docker", "exec", C.GATEWAY_NAME, "python", "-c", gprobe, check=False).stdout.strip().splitlines()
        check("gateway_egress_inference_only", len(g) == 4 and g[0].endswith("OPEN") and all("blocked" in x for x in g[1:]), g)
        gid = D.sh("docker", "exec", C.GATEWAY_NAME, "sh", "-c", "id -u; grep CapEff /proc/self/status | cut -f2", check=False).stdout.split()
        check("gateway_identity", gid[:1] == [str(C.GATEWAY_UID)] and gid[1:2] == ["0000000000000000"], gid)
        old_token, gw_egress_ip = job.token, D.ip_of(C.GATEWAY_NAME, C.EGRESS_NET)
        if compare:
            F.remove()
            r, _ = run_probe_container(job, "worker", C.IMAGES["worker"], [job.nets["cdp"], job.nets["gw"]], ["python", "-c", WORKER_PROBE], env, C.WORKER_UID)
            Q = json.loads(r.stdout.split("PROBE:", 1)[1]) if "PROBE:" in r.stdout else {}
            RESULTS["diagnostic_without_firewall"] = {"host_ports": Q.get("host_ports"), "metadata": Q.get("metadata"), "internet": Q.get("internet_1.1.1.1:80")}
            print("DIAG  without firewall:", json.dumps(RESULTS["diagnostic_without_firewall"]))
            F.install()
    rep = job.teardown_report
    check("teardown_removes_everything", all(v is True for k, v in rep.items() if k.endswith(("_removed", "_revoked"))) and not rep["leftover_containers"] and not rep["leftover_networks"], rep)
    try:
        J._NOPROXY.open(J.urllib.request.Request(f"http://{gw_egress_ip}:8080/v1/chat/completions", method="POST", data=b'{"model":"x"}',
                        headers={"Authorization": f"Bearer {old_token}", "Content-Type": "application/json"}), timeout=5); code = 200
    except J.urllib.error.HTTPError as e: code = e.code
    except Exception as e: code = f"unreachable:{type(e).__name__}"
    check("revoked_token_rejected_after_job", code == 401, code)
    # ---- crash: SIGKILL an orchestrator holding a job, then janitor ----
    p = subprocess.Popen([sys.executable, "-c", "import sys,time; sys.path.insert(0, %r); from afd import jobs as J\nj=J.Job(budget_usd=0.01,label='crash').__enter__()\nprint('UP', j.id, flush=True)\ntime.sleep(600)" % str(C.ROOT)],
                         stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline().strip(); crash_job = line.split()[1] if line.startswith("UP") else None
    os.kill(p.pid, signal.SIGKILL); p.wait()
    before = {"containers": D.labelled("container", f"afd.job={crash_job}"), "networks": D.labelled("network", f"afd.job={crash_job}")}
    removed = J.janitor()
    after = {"containers": D.labelled("container", f"afd.job={crash_job}"), "networks": D.labelled("network", f"afd.job={crash_job}")}
    check("janitor_cleans_after_orchestrator_sigkill", bool(before["containers"]) and not after["containers"] and not after["networks"],
          {"left_by_crash": before, "after_janitor": after, "tokens_revoked": len(removed["tokens"])})
    srv.shutdown()
    os.makedirs(C.VAR / "acceptance", exist_ok=True)
    json.dump({"at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pass": OK[0], "checks": RESULTS}, open(C.VAR / "acceptance" / "containment.json", "w"), indent=1)
    print("CONTAINMENT:", "PASS" if OK[0] else "FAIL"); sys.exit(0 if OK[0] else 1)

if __name__ == "__main__":
    main()
