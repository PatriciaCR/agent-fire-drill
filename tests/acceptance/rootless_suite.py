"""Rootless deployment security suite, ROOT side (no inference cost). Invoked by deploy/vm/verify.sh (AFD_ENV=vm) or, in the
development sandbox, by deploy/dev/rootless-verify.sh (AFD_ENV=dev). It checks the OS-level boundaries as root, runs the
orchestrator-side suite (containment_rootless.py) as afd-orch inside its namespaces, compares the worker's file scan with the
key, and writes $AFD_ACCEPTANCE/latest.json.

Every check has a status PASS / FAIL / SKIPPED and a `mandatory` flag. A check that cannot be evaluated in the current
environment is SKIPPED, never PASS. The overall result passes only if every mandatory check PASSED (none skipped), so a dev
emulation can never produce evidence that expose.sh accepts (it also requires environment == "vm")."""
import json, os, pwd, grp, re, socket, subprocess, sys, threading, time, http.server, socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ENV = os.environ.get("AFD_ENV", "vm")
ENV_FILE = Path(os.environ.get("AFD_ENV_FILE", "/etc/afd/afd.env"))
CFG = {k: v for k, v in (l.split("=", 1) for l in ENV_FILE.read_text().splitlines() if "=" in l and not l.lstrip().startswith("#"))}
KEY_SOURCE = Path(os.environ.get("AFD_KEY_SOURCE", "/etc/afd/vultr_key"))
GW, GWPORT = CFG.get("AFD_GATEWAY_ADDR", "10.233.0.1"), int(CFG.get("AFD_GATEWAY_PORT", "8080"))
STATE, VAR, ACC = Path(CFG["AFD_STATE"]), Path(CFG["AFD_VAR"]), Path(CFG["AFD_ACCEPTANCE"])
INFERENCE = CFG.get("AFD_INFERENCE_HOST", "api.vultrinference.com")
PROBE_PORT = 18080
ORCH, GWU, WEB = (pwd.getpwnam(u) for u in ("afd-orch", "afd-gw", "afd-web"))
RUN = f"/run/user/{ORCH.pw_uid}"
CHECKS = {}

def record(name, status, detail=None, mandatory=True):
    CHECKS[name] = {"status": status, "mandatory": mandatory, "detail": detail}
    print(f"{status:8} {name}  {json.dumps(detail, default=str)[:200] if detail is not None else ''}", flush=True)
def check(name, ok, detail=None, mandatory=True): record(name, "PASS" if ok else "FAIL", detail, mandatory)
def vm_only(name, fn, mandatory=True):
    if ENV != "vm": return record(name, "SKIPPED", "not evaluable outside the VM (needs systemd)", mandatory)
    try: fn()
    except Exception as e: record(name, "FAIL", f"{type(e).__name__}: {e}", mandatory)

def sh(*a, user=None, timeout=120, input=None):
    cmd = (["sudo", "-u", user, "-H", "env", f"XDG_RUNTIME_DIR={RUN}", f"DBUS_SESSION_BUS_ADDRESS=unix:path={RUN}/bus", f"AFD_ENV_FILE={ENV_FILE}",
            "PATH=/usr/bin:/bin:/usr/sbin:/sbin"] if user else []) + list(a)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
def in_ns(*a, timeout=300): return sh(str(ROOT / "deploy" / "vm" / "afd-ns"), *a, user="afd-orch", timeout=timeout)

CONNECT = "import socket,sys\nfor t in sys.argv[1:]:\n  h,p=t.rsplit(':',1)\n  try: socket.create_connection((h.strip('[]'),int(p)),timeout=3).close(); print(t,'OPEN')\n  except Exception as e: print(t,'blocked',type(e).__name__)"
def connect_as(user, targets):
    out = sh("python3", "-I", "-c", CONNECT, *targets, user=user).stdout
    return {l.split(" ")[0]: l.split(" ", 1)[1] for l in out.splitlines() if " " in l}

def host_ips():
    out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True).stdout
    return [m for m in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out) if m != GW]

def listener():
    class Q(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"host-service")
        def log_message(self, *a): pass
    socketserver.TCPServer.allow_reuse_address = True
    srv = socketserver.TCPServer(("0.0.0.0", PROBE_PORT), Q); threading.Thread(target=srv.serve_forever, daemon=True).start(); return srv

def gateway_pid():
    out = subprocess.run(["pgrep", "-u", "afd-gw", "-f", "gateway.py"], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None

def status_fields(pid):
    st = Path(f"/proc/{pid}/status").read_text()
    g = lambda k: next((l.split(":", 1)[1].split() for l in st.splitlines() if l.startswith(k + ":")), [])
    return {"Uid": g("Uid"), "CapEff": g("CapEff"), "NoNewPrivs": g("NoNewPrivs")}

def main():
    if os.geteuid() != 0: sys.exit("run as root")
    key = KEY_SOURCE.read_bytes().strip()
    key = key.split(b"=", 1)[1].strip().strip(b'"').strip(b"'") if b"=" in key else key
    if len(key) < 20: sys.exit("key source missing or too short")
    ips = host_ips(); srv = listener()
    inference_ip = socket.getaddrinfo(INFERENCE, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]

    # ---------------- identities and privilege ----------------
    sl = sh("sudo", "-l", "-U", "afd-orch").stdout
    nested = sh("sudo", "-n", "true", user="afd-orch")
    check("orch_has_no_sudo", "not allowed" in sl and nested.returncode != 0, {"sudo -l": sl.strip()[-120:], "sudo -n true rc": nested.returncode})
    bad = {"sudo", "docker", "adm", "lxd", "wheel", "admin", "root", "disk", "shadow", "systemd-journal"}
    memb = {u.pw_name: sorted({g.gr_name for g in grp.getgrall() if u.pw_name in g.gr_mem} | {grp.getgrgid(u.pw_gid).gr_name}) for u in (ORCH, GWU, WEB)}
    check("service_users_not_in_privileged_groups", all(not (set(v) & bad) for v in memb.values()), memb)
    r = sh("docker", "-H", "unix:///var/run/docker.sock", "ps", user="afd-orch")
    check("orch_cannot_use_rootful_docker_socket", r.returncode != 0, (r.stderr or r.stdout).strip()[-160:])
    def rootful_disabled():
        act = {u: sh("systemctl", "is-active", u).stdout.strip() for u in ("docker.service", "docker.socket", "containerd.service")}
        check("rootful_docker_disabled", not os.path.exists("/var/run/docker.sock") and all(v != "active" for v in act.values()), act)
    vm_only("rootful_docker_disabled", rootful_disabled)
    info = in_ns("docker", "info", "--format", "{{json .SecurityOptions}} {{.CgroupDriver}} {{.CgroupVersion}}").stdout.strip()
    dproc = subprocess.run(["ps", "-eo", "user:20,comm"], capture_output=True, text=True).stdout
    daemons = sorted({l.split()[0] for l in dproc.splitlines() if l.split()[-1] in ("dockerd", "rootlesskit", "containerd", "slirp4netns")})
    check("container_daemon_is_rootless_and_unprivileged", "rootless" in info and daemons == ["afd-orch"] or (ENV != "vm" and "rootless" in info and "afd-orch" in daemons),
          {"docker info": info, "daemon process owners": daemons, "note": None if ENV == "vm" else "dev emulation also runs the rootful development daemon"})
    vm_only("cgroup_driver_systemd_v2", lambda: check("cgroup_driver_systemd_v2", info.endswith("systemd 2"), info))
    def delegation():
        p = Path(f"/sys/fs/cgroup/user.slice/user-{ORCH.pw_uid}.slice/user@{ORCH.pw_uid}.service/cgroup.controllers")
        have = p.read_text().split() if p.exists() else []
        check("cgroup_controllers_delegated", {"cpu", "memory", "pids"} <= set(have), {"controllers": have})
    vm_only("cgroup_controllers_delegated", delegation)

    # ---------------- key custody ----------------
    stt = KEY_SOURCE.stat()
    readers = {u: sh("test", "-r", str(KEY_SOURCE), user=u).returncode == 0 for u in ("afd-orch", "afd-gw", "afd-web")}
    check("key_file_root_only", stt.st_uid == 0 and stt.st_gid == 0 and (stt.st_mode & 0o777) == 0o400 and not any(readers.values()),
          {"mode": oct(stt.st_mode & 0o777), "owner": f"{stt.st_uid}:{stt.st_gid}", "readable_by": readers})
    def creds():
        cd = "/run/credentials/afd-gateway.service/vultr_key"
        rd = {u: sh("test", "-r", cd, user=u).returncode == 0 for u in ("afd-orch", "afd-web")}
        check("gateway_credential_unreadable_by_other_services", os.path.exists(cd) and not any(rd.values()), {"credential": cd, "readable_by": rd})
    vm_only("gateway_credential_unreadable_by_other_services", creds)
    # (docker flags, entrypoint, args): every way afd-orch's rootless Docker could try to read the key off the host
    mounts = {"bind key dir": (["-v", f"{KEY_SOURCE.parent}:/x:ro"], "cat", [f"/x/{KEY_SOURCE.name}"]),
              "privileged + host root": (["--privileged", "-v", "/:/host:ro"], "cat", [f"/host{KEY_SOURCE}"]),
              "credentials dir": (["-v", "/run/credentials:/c:ro"], "sh", ["-c", "cat /c/*/vultr_key"])}
    got = {}
    for name, (flags, entry, args) in mounts.items():
        r = in_ns("docker", "run", "--rm", *flags, "--entrypoint", entry, "afd/gateway:1", *args)
        got[name] = {"rc": r.returncode, "leaked": key in (r.stdout + r.stderr).encode(), "stderr": r.stderr.strip()[-120:]}
    check("orch_cannot_obtain_key_through_its_docker", all(not v["leaked"] and v["rc"] != 0 for v in got.values()), got)
    pid = gateway_pid()
    if pid:
        envr = sh("cat", f"/proc/{pid}/environ", user="afd-orch"); memr = sh("head", "-c", "1", f"/proc/{pid}/mem", user="afd-orch")
        check("orch_cannot_read_gateway_process", envr.returncode != 0 and memr.returncode != 0 and key not in envr.stdout.encode(),
              {"environ_rc": envr.returncode, "mem_rc": memr.returncode})
        sf = status_fields(pid)
        check("gateway_process_identity", sf["Uid"][:1] == [str(GWU.pw_uid)] and sf["CapEff"] == ["0000000000000000"] and sf["NoNewPrivs"] == ["1"], sf)
        ss = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True).stdout
        mine = [l.split()[3] for l in ss.splitlines() if f"pid={pid}," in l]
        check("gateway_listens_only_on_gateway_address", mine == [f"{GW}:{GWPORT}"], mine)
    else:
        for n in ("orch_cannot_read_gateway_process", "gateway_process_identity", "gateway_listens_only_on_gateway_address"): record(n, "FAIL", "gateway process not found")
    def perms(p, mode, user, group):
        s = p.stat(); return {"path": str(p), "mode": oct(s.st_mode & 0o7777), "owner": pwd.getpwuid(s.st_uid).pw_name, "group": grp.getgrgid(s.st_gid).gr_name,
                              "ok": (s.st_mode & 0o7777) == mode and pwd.getpwuid(s.st_uid).pw_name == user and grp.getgrgid(s.st_gid).gr_name == group}
    pr = [perms(STATE / "tokens", 0o2750, "afd-orch", "afd-gw"), perms(STATE / "ledger", 0o2750, "afd-gw", "afd-orch"),
          perms(Path(CFG["AFD_RESULTS"]), 0o2750, "afd-orch", "afd-web"), perms(Path(CFG["AFD_SPOOL"]), 0o2770, "afd-web", "afd-orch")]
    check("state_directory_permissions", all(x["ok"] for x in pr), pr)

    # ---------------- host egress (the hard boundary) ----------------
    pub = ips[0] if ips else "203.0.113.1"
    t_orch = [f"{GW}:{GWPORT}", "1.1.1.1:443", f"{inference_ip}:443", "169.254.169.254:80", f"{pub}:22", f"{pub}:{PROBE_PORT}", f"127.0.0.1:{PROBE_PORT}", f"{GW}:22", "8.8.8.8:53"]
    o = connect_as("afd-orch", t_orch)
    check("orch_host_egress_gateway_only", o.get(f"{GW}:{GWPORT}") == "OPEN" and all(v.startswith("blocked") for k, v in o.items() if k != f"{GW}:{GWPORT}") and len(o) == len(t_orch), o)
    g = connect_as("afd-gw", [f"{inference_ip}:443", "1.1.1.1:443", "169.254.169.254:80", "8.8.8.8:53", f"{pub}:{PROBE_PORT}"])
    check("gateway_egress_inference_only", g.get(f"{inference_ip}:443") == "OPEN" and all(v.startswith("blocked") for k, v in g.items() if k != f"{inference_ip}:443"), g)
    w = connect_as("afd-web", [f"{GW}:{GWPORT}", "1.1.1.1:443", f"{inference_ip}:443", "169.254.169.254:80", f"{pub}:{PROBE_PORT}"])
    check("web_has_no_egress", len(w) == 5 and all(v.startswith("blocked") for v in w.values()), w)
    six = connect_as("afd-orch", ["[2606:4700:4700::1111]:443"])
    check("orch_ipv6_egress_blocked", all(v.startswith("blocked") for v in six.values()), six)
    r = subprocess.run(["python3", "-I", "-c", CONNECT, f"{pub}:{GWPORT}"], capture_output=True, text=True).stdout.strip()
    check("gateway_not_reachable_on_host_interfaces", "blocked" in r, r)
    ss = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True).stdout.split("\n")
    public = sorted({l.split()[3] for l in ss if l.strip() and not re.match(r"^(127\.|\[::1\]|::1|10\.233\.)", l.split()[3])})
    allowed = re.compile(r":(22|80|443)$")
    vm_only("no_unexpected_public_listeners", lambda: check("no_unexpected_public_listeners", all(allowed.search(a) for a in public), public))
    record("public_listeners", "PASS", public, mandatory=False)
    def boot_order():
        dropin = Path(f"/etc/systemd/system/user@{ORCH.pw_uid}.service.d/afd.conf")
        en = sh("systemctl", "is-enabled", "afd-firewall.service").stdout.strip()
        check("firewall_precedes_rootless_daemon_at_boot", dropin.exists() and "Requires=afd-firewall.service" in dropin.read_text() and en == "enabled", {"dropin": dropin.exists(), "afd-firewall": en})
    vm_only("firewall_precedes_rootless_daemon_at_boot", boot_order)

    # ---------------- orchestrator-side suite (inside the namespaces) ----------------
    r = in_ns("env", f"AFD_PROBE_HOST_IPS={','.join(ips)}", f"AFD_PROBE_HOST_PORT={PROBE_PORT}", "python3", "-I", str(ROOT / "tests" / "acceptance" / "containment_rootless.py"), timeout=1800)
    sys.stdout.write("".join("  orch> " + l + "\n" for l in r.stdout.splitlines())); sys.stdout.flush()
    orch_file = VAR / "acceptance" / "rootless_orch.json"
    try: orch = json.loads(orch_file.read_text())
    except Exception as e: orch = {"checks": {}}; record("orchestrator_suite_ran", "FAIL", f"{type(e).__name__}: {r.stderr[-400:]}")
    for k, v in orch["checks"].items(): record(f"orch:{k}", "PASS" if v["pass"] else "FAIL", v["detail"])
    blob_f = VAR / "acceptance" / "worker_files.bin"
    if blob_f.exists():
        blob = blob_f.read_bytes(); check("worker_key_absent_env_and_files", len(blob) > 0 and key not in blob, {"scanned_bytes": len(blob)}); blob_f.unlink()
    else: record("worker_key_absent_env_and_files", "FAIL", "no worker scan produced")
    # ---------------- key not written anywhere it should not be ----------------
    hits = []
    for base in (ROOT, STATE, Path("/tmp"), Path("/var/tmp"), *( [Path("/var/log")] if ENV == "vm" else [])):
        rr = subprocess.run(["grep", "-rlF", "--binary-files=text", "-e", key.decode(), str(base)], capture_output=True, text=True)
        hits += [h for h in rr.stdout.split() if Path(h) != KEY_SOURCE]
    if ENV == "vm":
        j = subprocess.run(["journalctl", "-u", "afd-gateway", "-u", "afd-web", "--no-pager", "-o", "cat"], capture_output=True, text=True).stdout
        if key.decode() in j: hits.append("journal")
    check("key_not_in_repo_state_tmp_or_logs", not hits, {"hits": hits})
    srv.shutdown()
    del key
    mand = {k: v for k, v in CHECKS.items() if v["mandatory"]}
    ok = ENV == "vm" and all(v["status"] == "PASS" for v in mand.values())
    commit = subprocess.run(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None
    out = {"at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "t": time.time(), "environment": ENV, "commit": commit, "pass": ok,
           "summary": {s: sum(v["status"] == s for v in mand.values()) for s in ("PASS", "FAIL", "SKIPPED")}, "checks": CHECKS}
    ACC.mkdir(parents=True, exist_ok=True)
    (ACC / "latest.json").write_text(json.dumps(out, indent=1, default=str)); os.chmod(ACC / "latest.json", 0o644)
    print(f"\nROOTLESS SUITE ({ENV}): {'PASS' if ok else 'NOT PASSED'}  mandatory: {out['summary']}"
          + ("" if ENV == "vm" else "  (dev emulation can never pass: VM-only checks are SKIPPED)"))
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
