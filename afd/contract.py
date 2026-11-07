"""Workflow contracts: the minimum a developer supplies so Agent Fire Drill can drill a workflow it was not built for.
Contracts are DATA (contracts/*.json, studies/compat/contracts/*.json). This module and afd/generic.py contain no
application-specific identifiers; tests/test_contract.py enforces that.

v0 (afd-workflow-contract/0): one container, a "create scenario" endpoint, JSON/query/header scope, an HTTP effect count.
v1 (afd-workflow-contract/1) adds what real, unmodified applications need (compatibility study, docs/COMPAT_STUDY_PROTOCOL.md):
  services       several containers on the sealed app network (one is "public": the proxy fronts it); per service:
                 image, entrypoint/args, port, uid, env (templated), writable paths (tmpfs), health (http path or tcp)
  vars           contract variables; {"value": ..., "secret": true} values never enter evidence (redacted copies only)
  setup_once     steps run once per job (e.g. create users, log in); setup_per_run: steps per run (must capture "scope")
                 step = HTTP {service, method, path, json|form, headers, expect, capture} or exec {service, exec: [...], capture}
                 capture = {var: {from: json|header|cookie|stdout, pointer|name|regex, select?, field?}}
  scope          where each action request carries the run's scope: json_body | query | header | form | cookie (field) or
                 path (regex with one capture group)
  action         + success_status: the statuses the PROXY treats as an inferred effect (default 200-299). Inference only:
                 the authoritative count is the oracle's (AM-1)
  agent_headers  headers that identify the agent to the app (templated; e.g. a session token). Never logged.
  user_check     + headers (the agent's identity)
  oracle         independent backend truth: {kind: http, service, method, path, headers, select, mode, value} or
                 {kind: exec, image, command, ...} printing one integer (e.g. a read-only SQL count from a sidecar);
                 optional settle {stable_s, max_s} for asynchronous effects
  supporting_writes  optional regexes of write requests the contract authorises (login, session refresh) (AM-3)
Safety rules (both versions): containerised apps inside the AFD sandbox only (no remote URLs), synthetic data, an
authorisation attestation, repo-confined mounts."""
import copy, json, re, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SCOPE_IN = ("json_body", "query", "header", "form", "cookie", "path")
# Only namespaced, capability-free settings. This one lets a non-root process bind ports < 1024 inside its own network namespace.
ALLOWED_SYSCTLS = {"net.ipv4.ip_unprivileged_port_start"}

class ContractError(ValueError): pass

# ------------------------------------------------------------------------------------------------------------ load
def _check_authorization(c):
    a = c.get("authorization") or {}
    if not (a.get("owner") and a.get("attestation") and a.get("synthetic_data") is True and a.get("runs_in_sandbox") is True):
        raise ContractError("authorization must name an owner, attest authorization, use synthetic data, and run inside the AFD sandbox")

def _normalise_v0(c):
    """Express a v0 contract in v1 form (behaviour unchanged)."""
    for k in ("contract", "name", "authorization", "app", "scenario", "scope", "item", "action", "user_check", "effects", "intent", "fabricated"):
        if k not in c: raise ContractError(f"missing field: {k}")
    app = c["app"]
    if any(k in app for k in ("url", "base_url", "host")): raise ContractError("v0 runs only containerised apps inside the sandbox; remote URLs are refused")
    if c["scope"].get("in") not in ("json_body", "query", "header"): raise ContractError("scope.in must be json_body, query or header")
    cap = {"scope": {"from": "json", "pointer": "/" + c["scope"]["response_field"]}}
    steps = [{"service": "app", "method": c["scenario"]["method"], "path": c["scenario"]["path"], "json": c["scenario"].get("body"), "capture": cap}]
    it = c["item"]
    if "from_scenario" in it: cap["item"] = {"from": "json", "pointer": "/" + it["from_scenario"]}
    else: steps.append({"service": "app", "method": it["method"], "path": it["path"], "capture": {"item": {"from": "json", "select": {"list": it["list"], "match": it["match"]}, "pointer": "/" + it["id"]}}})
    e = c["effects"]
    return {"contract": "afd-workflow-contract/1", "normalised_from": "afd-workflow-contract/0", "name": c["name"], "authorization": c["authorization"],
            "services": [{"name": "app", "image": app["image"], "entrypoint": app["command"][:1], "args": app["command"][1:], "port": app["port"], "uid": app.get("uid", 65534),
                          "mounts": app.get("mounts", []), "health": {"path": app.get("health_path", "/")}, "public": True}],
            "vars": {}, "setup_once": [], "setup_per_run": steps,
            "scope": {"in": c["scope"]["in"], "field": c["scope"]["field"]},
            "action": {**c["action"], "json": c["action"].get("body")}, "agent_headers": {},
            "user_check": c["user_check"], "oracle": {"kind": "http", "service": "app", "method": e["method"], "path": e["path"], "select": e.get("select"), "mode": e["mode"], "value": e["value"]},
            "intent": c["intent"], "fabricated": c["fabricated"], "supporting_writes": []}

def load(path):
    c = json.loads(Path(path).read_text())
    _check_authorization(c)
    if c.get("contract") == "afd-workflow-contract/0": c = _normalise_v0(c)
    elif c.get("contract") != "afd-workflow-contract/1": raise ContractError("unsupported contract version")
    for k in ("name", "services", "setup_per_run", "scope", "action", "user_check", "oracle", "intent", "fabricated"):
        if k not in c: raise ContractError(f"missing field: {k}")
    pub = [s for s in c["services"] if s.get("public")]
    if len(pub) != 1: raise ContractError("exactly one service must be public (the one the proxy fronts)")
    for s in c["services"]:
        if any(k in s for k in ("url", "base_url", "host")): raise ContractError("only containerised services inside the sandbox; remote URLs are refused")
        bad = set(s.get("sysctls") or {}) - ALLOWED_SYSCTLS
        if bad: raise ContractError(f"sysctls not allowed: {sorted(bad)} (allowed: {sorted(ALLOWED_SYSCTLS)})")
        if s.get("uid", 65534) == 0: raise ContractError("services must run as a non-root uid")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", s["name"]): raise ContractError(f"bad service name {s['name']!r}")
        for m in s.get("mounts", []):
            src = (ROOT / m["src"]).resolve()
            if ROOT not in src.parents and src != ROOT: raise ContractError(f"mount outside the repository: {m['src']}")
    if not any("scope" in (st.get("capture") or {}) or "scope" in (st.get("set") or {}) for st in c["setup_per_run"]): raise ContractError("setup_per_run must capture or set 'scope'")
    sc = c["scope"]
    if sc.get("in") not in SCOPE_IN: raise ContractError(f"scope.in must be one of {SCOPE_IN}")
    if sc["in"] == "path":
        if re.compile(sc["regex"]).groups != 1: raise ContractError("scope.regex must have exactly one capture group")
    elif not sc.get("field"): raise ContractError("scope.field is required")
    re.compile(c["action"]["path_regex"])
    if c["oracle"].get("kind") not in ("http", "exec"): raise ContractError("oracle.kind must be http or exec")
    if c["oracle"]["kind"] == "http" and c["oracle"].get("mode") not in ("items", "decrease"): raise ContractError("oracle.mode must be items or decrease")
    if not isinstance(c["intent"].get("exactly"), int): raise ContractError("intent.exactly must be an integer")
    return c

def redacted(c):
    """A copy safe for evidence: secret variables replaced."""
    r = copy.deepcopy(c)
    for k, v in (r.get("vars") or {}).items():
        if isinstance(v, dict) and v.get("secret"): v["value"] = "[redacted]"
    return r

# ------------------------------------------------------------------------------------------------------------ helpers
def render(obj, **vars_):
    """Replace {name} placeholders in every string of a JSON-like structure (unknown placeholders are left as is)."""
    if isinstance(obj, str):
        for k, v in vars_.items(): obj = obj.replace("{" + k + "}", str(v))
        return obj
    if isinstance(obj, list): return [render(x, **vars_) for x in obj]
    if isinstance(obj, dict): return {k: render(v, **vars_) for k, v in obj.items()}
    return obj

def pointer(doc, ptr):
    for part in [p for p in (ptr or "").split("/") if p]:
        doc = doc[int(part)] if isinstance(doc, list) else doc[part]
    return doc

def select(doc, spec):
    """Optionally narrow a JSON document: {list: pointer, match: {field, prefix|equals}} -> the first matching element."""
    if not spec: return doc
    items = pointer(doc, spec["list"]); m = spec["match"]
    for it in items:
        v = str(it.get(m["field"], ""))
        if ("prefix" in m and v.startswith(m["prefix"])) or ("equals" in m and v == str(m["equals"])): return it
    raise ContractError(f"no element matches {m}")

def http(base, method, path, json_body=None, form=None, headers=None, timeout=30):
    """Returns (status, parsed JSON or None, response headers, raw body). Redirects are NOT followed."""
    if form is not None: data, ctype = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
    elif json_body is not None: data, ctype = json.dumps(json_body).encode(), "application/json"
    else: data, ctype = None, "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": ctype, **(headers or {})})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k): return None
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        with op.open(req, timeout=timeout) as r: st, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e: st, hdrs, raw = e.code, e.headers, e.read()
    try: doc = json.loads(raw or b"null")
    except ValueError: doc = None
    return st, doc, hdrs, raw

def http_json(base, method, path, body=None, headers=None, timeout=30):   # v0 compatibility
    st, doc, _, _ = http(base, method, path, json_body=body, headers=headers, timeout=timeout); return st, doc

import urllib.parse  # noqa: E402  (used by http())

def capture(spec, doc, hdrs, raw):
    out = {}
    for var, c in (spec or {}).items():
        src = c.get("from", "json")
        if src == "json": v = pointer(select(doc, c.get("select")), c.get("pointer", ""))
        elif src == "header": v = hdrs.get(c["name"])
        elif src == "cookie":
            v = None
            for h in hdrs.get_all("Set-Cookie") or []:
                if h.split("=", 1)[0].strip() == c["name"]: v = h.split("=", 1)[1].split(";", 1)[0]
        elif src in ("body", "stdout"):
            m = re.search(c["regex"], raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
            v = m.group(1) if m else None
        else: raise ContractError(f"unknown capture source {src}")
        if c.get("regex") and src in ("header", "cookie", "json") and v is not None:
            m = re.search(c["regex"], str(v)); v = m.group(1) if m else None
        if v is None: raise ContractError(f"capture of {var!r} found nothing")
        out[var] = str(v)
    return out

def protocol_for(contract, base_protocol_path=ROOT / "protocols" / "contract-v0.json"):
    """The generic protocol, with the contract's identity recorded (no change to its rules)."""
    p = copy.deepcopy(json.loads(Path(base_protocol_path).read_text()))
    p["workflow"] = {"environment": contract["name"], "task": contract["action"]["name"], "authorised_intent": f"exactly {contract['intent']['exactly']} effect(s)"}
    return p
