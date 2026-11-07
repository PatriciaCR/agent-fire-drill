"""Host firewall for Agent Fire Drill (iptables via the iptables-nft backend Docker uses). Defence in depth beyond Docker's
internal networks, which still give the host an address on each bridge. Two dedicated chains, idempotently (re)installed:
  AFD-FWD (jumped to from DOCKER-USER, i.e. forwarded container traffic):
      established/related  -> RETURN
      gateway egress subnet -> TCP 443 to the resolved Vultr inference addresses: RETURN; anything else: DROP
      any source           -> 169.254.169.254 (cloud metadata): DROP
      job subnet pool      -> outside the pool: DROP
  AFD-IN (jumped to from INPUT, i.e. traffic addressed to the host itself):
      established/related  -> RETURN (replies to connections the orchestrator opened)
      job subnet pool / gateway egress subnet -> host: DROP"""
import socket, subprocess
from . import config as C

def ipt(*args, check=True):
    r = subprocess.run(["sudo", "-n", "iptables", "-w", *args], capture_output=True, text=True)
    if check and r.returncode != 0: raise RuntimeError(f"iptables {' '.join(args)} failed: {r.stderr.strip()}")
    return r

def resolve_inference():
    return sorted({ai[4][0] for ai in socket.getaddrinfo(C.INFERENCE_HOST, 443, socket.AF_INET, socket.SOCK_STREAM)})

def _chain(name):
    if ipt("-n", "-L", name, check=False).returncode != 0: ipt("-N", name)
    ipt("-F", name)

def _jump(parent, chain):
    while ipt("-C", parent, "-j", chain, check=False).returncode == 0: ipt("-D", parent, "-j", chain)
    ipt("-I", parent, "1", "-j", chain)

def install():
    ips = resolve_inference()
    _chain("AFD-FWD"); _chain("AFD-IN")
    f = lambda *a: ipt("-A", "AFD-FWD", *a)
    f("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN")
    for ip in ips: f("-s", C.EGRESS_SUBNET, "-d", ip, "-p", "tcp", "--dport", "443", "-j", "RETURN")
    f("-s", C.EGRESS_SUBNET, "-j", "DROP")
    f("-d", "169.254.169.254", "-j", "DROP")
    f("-s", C.JOB_POOL, "!", "-d", C.JOB_POOL, "-j", "DROP")
    g = lambda *a: ipt("-A", "AFD-IN", *a)
    g("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN")
    g("-s", C.JOB_POOL, "-j", "DROP"); g("-s", C.EGRESS_SUBNET, "-j", "DROP")
    _jump("DOCKER-USER", "AFD-FWD"); _jump("INPUT", "AFD-IN")
    return {"inference_ips": ips}

def remove():
    for parent, chain in (("DOCKER-USER", "AFD-FWD"), ("INPUT", "AFD-IN")):
        while ipt("-C", parent, "-j", chain, check=False).returncode == 0: ipt("-D", parent, "-j", chain)
        if ipt("-n", "-L", chain, check=False).returncode == 0: ipt("-F", chain); ipt("-X", chain)

def show():
    return "\n".join(ipt("-S", c, check=False).stdout for c in ("AFD-FWD", "AFD-IN"))

# ---- rootless mode: rules INSIDE the rootless Docker network namespace -------------------------------------------------
# The orchestrator runs as (namespaced) root in that namespace, so it can manage these rules without sudo. They are defence
# in depth only: the hard boundary is the root-owned host rule (deploy/vm/afd-firewall.sh) that limits every socket the
# afd-orch user (slirp4netns included) opens on the host to the gateway address and port.
#   AFD-NS    (jumped to from DOCKER-USER): job pool -> gateway addr:port allowed; established allowed; anything else from the pool dropped
#   AFD-NS-IN (jumped to from INPUT):       job pool -> the namespace itself dropped (except established)
def _ipt_ns(*args, check=True):
    r = subprocess.run(["iptables", "-w", *args], capture_output=True, text=True)
    if check and r.returncode != 0: raise RuntimeError(f"iptables(netns) {' '.join(args)} failed: {r.stderr.strip()}")
    return r

def ensure_netns():
    if _ipt_ns("-n", "-L", "DOCKER-USER", check=False).returncode != 0: _ipt_ns("-N", "DOCKER-USER")
    for parent, chain in (("DOCKER-USER", "AFD-NS"), ("INPUT", "AFD-NS-IN")):
        if _ipt_ns("-n", "-L", chain, check=False).returncode != 0: _ipt_ns("-N", chain)
        _ipt_ns("-F", chain)
    a = lambda *x: _ipt_ns("-A", "AFD-NS", *x)
    a("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN")
    a("-s", C.JOB_POOL, "-d", C.GATEWAY_ADDR, "-p", "tcp", "--dport", str(C.GATEWAY_PORT), "-j", "RETURN")
    a("-s", C.JOB_POOL, "!", "-d", C.JOB_POOL, "-j", "DROP")
    b = lambda *x: _ipt_ns("-A", "AFD-NS-IN", *x)
    b("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN")
    b("-s", C.JOB_POOL, "-j", "DROP")
    for parent, chain in (("DOCKER-USER", "AFD-NS"), ("INPUT", "AFD-NS-IN")):
        if _ipt_ns("-C", parent, "-j", chain, check=False).returncode != 0: _ipt_ns("-I", parent, "1", "-j", chain)

def show_netns():
    return "\n".join(_ipt_ns("-S", c, check=False).stdout for c in ("AFD-NS", "AFD-NS-IN"))
