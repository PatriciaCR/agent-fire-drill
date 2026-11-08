"""External-agent spec (afd-agent-spec/1): how a developer's OWN agent is run as the candidate under evaluation, without
AFD source changes. Pure validation and result parsing (no Docker here; afd/generic.py:GenericJob.run_external runs it).

  {
    "agent_spec": "afd-agent-spec/1",
    "name": "my-agent",                                   # [a-z0-9-], becomes the candidate "external:my-agent"
    "image": "registry/name@sha256:<64 hex>",              # digest-pinned, present locally (never pulled)
    "command": ["entrypoint", "arg", ...],                 # run as is (no shell added); <= 64 items
    "env": {"MY_SETTING": "value"},                       # ALLOWLIST: only these literal variables are passed
    "credentials": ["agent_password"],                    # optional: contract variables exposed as AFD_CRED_<NAME>
    "task": {"instruction": "...", "start_path": "/projects/{scope}"},
    "result": {"mechanism": "stdout_marker" | "exit_code"},
    "timeout_s": 300,                                     # 10..900
    "uid": 10002                                          # optional, non-root (default: the AFD worker uid)
  }

What AFD gives the agent (environment; nothing else is set): AFD_TARGET_URL (always http://afd-proxy:8080, the ONLY
address that reaches the app), AFD_START_URL (target + start_path, templated with {scope} {decoy} {target} {run_nonce}),
AFD_TASK (the instruction, templated the same way), AFD_RUN_ID, the spec's env allowlist, and the declared credentials.
The container joins the isolated agent network only: no internet, no gateway, no direct app access; read-only root
filesystem, no capabilities, non-root, resource limits (same hardening as AFD's own workers).

Result mechanisms (documented contract with the agent):
  stdout_marker  the agent prints a line  AFD_RESULT_JSON={"claimed_success": true|false, "summary": "..."}  (the last
                 such line counts); a missing line, invalid JSON or a non-boolean claimed_success is a MALFORMED result
  exit_code      exit status 0 = claims success, any other status = claims failure (for unmodified test runners)
A timeout, a missing or a malformed result is never a claim: the run fails the evidence gate."""
import json, re

SPEC_VERSION = "afd-agent-spec/1"
PINNED = re.compile(r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV = re.compile(r"^(AFD_|HTTPS?_PROXY$|NO_PROXY$|ALL_PROXY$|LD_|DOCKER_)", re.I)
URL = re.compile(r"\b([a-z][a-z0-9+.-]*)://(?:[^/@\s]*@)?([^/:?#\s\"'{}]+)", re.I)
TEMPLATE_VARS = ("scope", "decoy", "target", "run_nonce")
MARKER = "AFD_RESULT_JSON="
PROXY_HOST = "afd-proxy"

class SpecError(ValueError): pass

def check(spec, contract=None):
    """Returns a list of problems (empty = acceptable). contract: the loaded contract, to check declared credentials."""
    p = []
    if spec.get("agent_spec") != SPEC_VERSION: p.append(f"agent_spec must be {SPEC_VERSION!r}")
    allowed = {"agent_spec", "name", "image", "command", "env", "credentials", "task", "result", "timeout_s", "uid", "note"}
    if set(spec) - allowed: p.append(f"unknown keys: {sorted(set(spec) - allowed)} (no mounts, ports, networks, privileges or capabilities)")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", str(spec.get("name", ""))): p.append("name must match [a-z0-9][a-z0-9-]{0,40}")
    if not PINNED.match(str(spec.get("image", ""))): p.append("image must be pinned by digest (name@sha256:<64 hex>)")
    cmd = spec.get("command")
    if not (isinstance(cmd, list) and 1 <= len(cmd) <= 64 and all(isinstance(x, str) and len(x) <= 16384 for x in cmd)): p.append("command must be a list of 1-64 strings")
    env = spec.get("env") or {}
    if not isinstance(env, dict): p.append("env must be an object"); env = {}
    for k, v in env.items():
        if not ENV_KEY.match(k): p.append(f"env name not allowed: {k!r}")
        elif RESERVED_ENV.match(k): p.append(f"env name reserved: {k!r}")
        if not isinstance(v, str) or len(v) > 4096: p.append(f"env {k!r} must be a string of <= 4096 characters")
    task = spec.get("task") or {}
    if not (isinstance(task.get("instruction"), str) and task["instruction"].strip()): p.append("task.instruction is required")
    sp = task.get("start_path", "/")
    if not (isinstance(sp, str) and sp.startswith("/") and "://" not in sp): p.append("task.start_path must be a path on the app (starting with '/')")
    for field in ("instruction", "start_path"):
        for ph in re.findall(r"\{([a-z_]+)\}", str(task.get(field, ""))):
            if ph not in TEMPLATE_VARS: p.append(f"task.{field} placeholder {{{ph}}} not allowed (allowed: {', '.join(TEMPLATE_VARS)})")
    if (spec.get("result") or {}).get("mechanism") not in ("stdout_marker", "exit_code"): p.append("result.mechanism must be stdout_marker or exit_code")
    t = spec.get("timeout_s", 300)
    if not (isinstance(t, int) and 10 <= t <= 900): p.append("timeout_s must be an integer in [10, 900]")
    if "uid" in spec and not (isinstance(spec["uid"], int) and spec["uid"] > 0): p.append("uid must be a non-root integer")
    creds = spec.get("credentials") or []
    if not (isinstance(creds, list) and all(isinstance(c, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,40}", c) for c in creds)): p.append("credentials must be contract variable names")
    elif contract is not None:
        known = set((contract.get("vars") or {})) | {k for st in contract.get("setup_once", []) + contract.get("setup_per_run", []) for k in (st.get("capture") or {})}
        missing = [c for c in creds if c not in known]
        if missing: p.append(f"credentials not defined by the contract: {missing}")
    def scan(where, s):
        for scheme, host in URL.findall(s):
            if host.lower() != PROXY_HOST and not host.startswith("{"): p.append(f"remote URL refused in {where}: {scheme}://{host}")
    for i, x in enumerate(cmd if isinstance(cmd, list) else []): scan(f"command[{i}]", str(x))
    for k, v in env.items(): scan(f"env.{k}", str(v))
    scan("task", json.dumps(task))
    return p

def render(text, vars_):
    out = str(text)
    for k in TEMPLATE_VARS:
        if k in vars_: out = out.replace("{" + k + "}", str(vars_[k]))
    return out

def parse_result(mechanism, exit_code, stdout, timed_out):
    """-> {result_valid, claimed_success (bool|None), summary, problem}. Never infers a claim from a missing result."""
    if timed_out: return {"result_valid": False, "claimed_success": None, "summary": None, "problem": "agent timed out (killed)"}
    if mechanism == "exit_code":
        if exit_code is None: return {"result_valid": False, "claimed_success": None, "summary": None, "problem": "no exit status"}
        return {"result_valid": True, "claimed_success": exit_code == 0, "summary": f"exit status {exit_code}", "problem": None}
    lines = [l for l in (stdout or "").splitlines() if l.startswith(MARKER)]
    if not lines: return {"result_valid": False, "claimed_success": None, "summary": None, "problem": f"missing result: no line starting with {MARKER}"}
    try: doc = json.loads(lines[-1][len(MARKER):])
    except ValueError: return {"result_valid": False, "claimed_success": None, "summary": None, "problem": "malformed result: invalid JSON after the marker"}
    if not isinstance(doc, dict) or not isinstance(doc.get("claimed_success"), bool):
        return {"result_valid": False, "claimed_success": None, "summary": None, "problem": "malformed result: claimed_success must be true or false"}
    return {"result_valid": True, "claimed_success": doc["claimed_success"], "summary": str(doc.get("summary", ""))[:2000], "problem": None}

def redact(text, secrets, limit=32768):
    t = (text or "")[-limit:]
    for s in sorted({x for x in secrets if x and len(x) > 4}, key=len, reverse=True): t = t.replace(s, "[redacted]")
    return t
