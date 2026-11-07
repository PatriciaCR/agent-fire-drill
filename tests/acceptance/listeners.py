"""Listener helpers for the rootless security suite (importable without root; unit-tested in tests/test_listeners.py).

The suite needs a temporary host service (the "probe") to prove that afd-orch, afd-gw, afd-web and the containers cannot
reach host ports. Rules:
  - the probe is guarded BEFORE it binds: a root-owned INPUT chain (AFD-PROBE) drops any packet to the probe port that does
    not arrive on loopback (IPv4 and IPv6). Every probe in the suite is local (it arrives on lo; container traffic leaves
    the rootless namespace through slirp4netns as a local socket), so the guard changes nothing the suite measures, but
    the probe is never reachable from outside the host;
  - the public-listener check excludes ONLY the suite's own probe socket (same port AND owned solely by this process).
    Anything else, including another process on the probe port, is still reported as unexpected;
  - the probe is always shut down and the guard always removed (context manager), and the suite checks both afterwards."""
import contextlib, http.server, re, socketserver, subprocess, threading

ALLOWED_PUBLIC_PORTS = (22, 80, 443)
LOCAL = re.compile(r"^(127\.|\[::1\]|::1|\[::ffff:127\.|10\.233\.)")
CHAIN = "AFD-PROBE"

def parse_ss(text):
    """`ss -ltnpH` output -> [(local address, frozenset of owning pids)]."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4: continue
        out.append((f[3], frozenset(int(p) for p in re.findall(r"pid=(\d+)", line))))
    return out

def port_of(addr): return int(addr.rsplit(":", 1)[1])

def classify_public(listeners, own_probe=None, allowed=ALLOWED_PUBLIC_PORTS):
    """Return (unexpected, excluded_own_probe, allowed_seen) for non-loopback listeners.
    own_probe = (port, pid): excluded only if the socket is on that port and owned by exactly that pid."""
    unexpected, excluded, ok = [], [], []
    for addr, pids in listeners:
        if LOCAL.match(addr): continue
        if own_probe and port_of(addr) == own_probe[0] and pids == frozenset({own_probe[1]}): excluded.append(addr); continue
        (ok if port_of(addr) in allowed else unexpected).append(addr)
    return sorted(set(unexpected)), sorted(set(excluded)), sorted(set(ok))

def listeners_now():
    return parse_ss(subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True).stdout)

# ---- external-interface guard (root) ----
def _ipt(tool, *a): return subprocess.run([tool, "-w", *a], capture_output=True, text=True)

def guard_install(port):
    for t in ("iptables", "ip6tables"):
        if _ipt(t, "-n", "-L", CHAIN).returncode != 0: _ipt(t, "-N", CHAIN)
        _ipt(t, "-F", CHAIN)
        r = _ipt(t, "-A", CHAIN, "-p", "tcp", "--dport", str(port), "!", "-i", "lo", "-j", "DROP")
        if r.returncode != 0: raise RuntimeError(f"{t}: cannot install probe guard: {r.stderr.strip()}")
        while _ipt(t, "-C", "INPUT", "-j", CHAIN).returncode == 0: _ipt(t, "-D", "INPUT", "-j", CHAIN)
        if _ipt(t, "-I", "INPUT", "1", "-j", CHAIN).returncode != 0: raise RuntimeError(f"{t}: cannot hook probe guard")

def guard_active(port):
    return all(_ipt(t, "-C", "INPUT", "-j", CHAIN).returncode == 0 and
               _ipt(t, "-C", CHAIN, "-p", "tcp", "--dport", str(port), "!", "-i", "lo", "-j", "DROP").returncode == 0 for t in ("iptables", "ip6tables"))

def guard_remove(port=None):
    for t in ("iptables", "ip6tables"):
        while _ipt(t, "-C", "INPUT", "-j", CHAIN).returncode == 0: _ipt(t, "-D", "INPUT", "-j", CHAIN)
        if _ipt(t, "-n", "-L", CHAIN).returncode == 0: _ipt(t, "-F", CHAIN); _ipt(t, "-X", CHAIN)

def guard_absent():
    return all(_ipt(t, "-n", "-L", CHAIN).returncode != 0 for t in ("iptables", "ip6tables"))

@contextlib.contextmanager
def probe_listener(port, install=guard_install, remove=guard_remove):
    """Guarded temporary host service on 0.0.0.0:port. The guard goes up before the bind and comes down after the close."""
    class Q(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"host-service")
        def log_message(self, *a): pass
    class S(socketserver.ThreadingTCPServer):
        allow_reuse_address = True; daemon_threads = True
    srv = None
    try:
        install(port)
        srv = S(("0.0.0.0", port), Q)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield srv
    finally:
        if srv is not None:
            srv.shutdown(); srv.server_close()
        remove(port)
