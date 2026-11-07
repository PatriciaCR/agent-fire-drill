"""Gateway lifecycle and job tokens (orchestrator side). The orchestrator writes token files and reads the ledger; it never
reads the key file (the key file is 0400 owned by the gateway uid, and the orchestrator runs as a different user)."""
import hashlib, json, os, secrets, time
from . import config as C, dockerops as D

def url():
    """Base URL the worker uses (rootful: set per job from the gateway's IP on the job network)."""
    return f"http://{C.GATEWAY_ADDR}:{C.GATEWAY_PORT}"

def ensure_dirs():
    C.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if C.ROOTLESS:   # created by deploy/vm/install.sh (root); the orchestrator has no sudo and only checks them
        for d in (C.TOKEN_DIR, C.LEDGER_DIR):
            if not d.is_dir(): raise RuntimeError(f"{d} missing: run deploy/vm/install.sh as root")
        if not os.access(C.TOKEN_DIR, os.W_OK): raise RuntimeError(f"{C.TOKEN_DIR} not writable by the orchestrator")
        return
    me = f"{os.getuid()}:{os.getgid()}"
    D.sh("sudo", "-n", "mkdir", "-p", str(C.TOKEN_DIR), str(C.LEDGER_DIR), str(C.SECRET_DIR))
    D.sh("sudo", "-n", "chown", "root:root", str(C.STATE)); D.sh("sudo", "-n", "chmod", "0755", str(C.STATE))
    D.sh("sudo", "-n", "chown", me, str(C.TOKEN_DIR)); D.sh("sudo", "-n", "chmod", "0755", str(C.TOKEN_DIR))   # orchestrator writes tokens
    D.sh("sudo", "-n", "chown", "root:root", str(C.SECRET_DIR)); D.sh("sudo", "-n", "chmod", "0711", str(C.SECRET_DIR))
    # ledger: writable by the gateway uid only; setgid to the orchestrator's group so the gateway's 0640 rows are readable by it
    D.sh("sudo", "-n", "chown", f"{C.GATEWAY_UID}:{os.getgid()}", str(C.LEDGER_DIR)); D.sh("sudo", "-n", "chmod", "2750", str(C.LEDGER_DIR))
    D.sh("sudo", "-n", "find", str(C.LEDGER_DIR), "-type", "f", "-exec", "chgrp", str(os.getgid()), "{}", "+")

def install_key(source_env_file):
    """One-time admin step: copy the key into SECRET_DIR/vultr_key as 0400 owned by the gateway uid. Never printed.
    Afterwards the orchestrator user itself can no longer read it."""
    raw = open(os.path.expanduser(source_env_file)).read()
    key = next((l.split("=", 1)[1].strip().strip('"').strip("'") for l in raw.splitlines() if l.strip().startswith("VULTR_INFERENCE_API_KEY=")), raw.strip())
    ensure_dirs()
    D.sh("sudo", "-n", "sh", "-c", f"umask 377; cat > '{C.KEY_FILE}'", input=key + "\n")
    D.sh("sudo", "-n", "chown", f"{C.GATEWAY_UID}:{C.GATEWAY_UID}", str(C.KEY_FILE)); D.sh("sudo", "-n", "chmod", "0400", str(C.KEY_FILE))
    del key, raw
    return {"key_file": str(C.KEY_FILE), "orchestrator_can_read": os.access(C.KEY_FILE, os.R_OK)}

def up():
    """Rootful: start (or keep) the long-lived gateway container: egress network only; key mounted read-only; tokens read-only.
    Rootless: the gateway is the host service afd-gateway.service; only check that it answers."""
    ensure_dirs()
    if C.ROOTLESS:
        from .jobs import http_json
        for _ in range(20):
            try:
                if http_json(url() + "/healthz", timeout=3).get("ok") is True: return url()
            except Exception: time.sleep(0.5)
        raise RuntimeError(f"gateway service not reachable at {url()} (systemctl status afd-gateway)")
    if not C.KEY_FILE.exists(): raise RuntimeError(f"key file missing: {C.KEY_FILE} (install with `afd install-key`)")
    if not D.exists("network", C.EGRESS_NET):
        D.sh("docker", "network", "create", "--driver", "bridge", "--subnet", C.EGRESS_SUBNET, *D.labels(None, "egress"), C.EGRESS_NET)
    if D.exists("container", C.GATEWAY_NAME):
        if D.inspect(C.GATEWAY_NAME)["State"]["Running"]: return C.GATEWAY_NAME
        D.remove_container(C.GATEWAY_NAME)
    D.sh("docker", "run", "-d", "--name", C.GATEWAY_NAME, "--network", C.EGRESS_NET, *D.labels(None, "gateway"),
         *D.hardening("gateway", C.GATEWAY_UID), "--restart", "unless-stopped",
         "-v", f"{C.KEY_FILE}:/run/secrets/vultr_key:ro", "-v", f"{C.TOKEN_DIR}:/run/afd/tokens:ro", "-v", f"{C.LEDGER_DIR}:/var/lib/afd/ledger",
         C.IMAGES["gateway"])
    return C.GATEWAY_NAME

def down():
    if C.ROOTLESS: return
    D.remove_container(C.GATEWAY_NAME); D.remove_network(C.EGRESS_NET)

def issue_token(job_id, budget_usd):
    tok = secrets.token_hex(32)
    p = C.TOKEN_DIR / (hashlib.sha256(tok.encode()).hexdigest() + ".json")
    p.write_text(json.dumps({"job_id": job_id, "budget_usd": budget_usd, "models": [C.MODEL], "max_tokens": C.MAX_TOKENS, "issued": time.time()}))
    # rootful dev: 0644 (the gateway container's uid must read it; limitation L4). Rootless: 0640 in a setgid directory
    # owned afd-orch:afd-gw, so only the orchestrator and the gateway can read live tokens.
    os.chmod(p, 0o640 if C.ROOTLESS else 0o644)
    return tok, p

def revoke(token_path):
    try: os.remove(token_path)
    except FileNotFoundError: pass
    return not os.path.exists(token_path)

def job_spend(job_id):
    p = C.LEDGER_DIR / f"{job_id}.jsonl"
    if not p.exists(): return {"calls": 0, "cost_usd": 0.0, "estimated_calls": 0, "budget_refusals": 0, "prompt_tokens": 0, "completion_tokens": 0, "truncated": 0}
    allrows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    rows = [r for r in allrows if not r.get("refused")]
    return {"calls": len(rows), "budget_refusals": sum(r.get("refused") == "budget" for r in allrows), "cost_usd": round(sum(r["cost_usd"] for r in rows), 6), "estimated_calls": sum(bool(r.get("estimated_upper_bound")) for r in rows),
            "prompt_tokens": sum(r["prompt_tokens"] for r in rows), "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "truncated": sum(r.get("finish_reason") == "length" for r in rows)}
