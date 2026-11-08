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
v1.1 (afd-workflow-contract/1.1, protocol contract-v1) = v1 plus a required "objects" section (per-object accounting, M1):
  objects.id         where a watched write names the object it changes: same forms as scope (json_body | query | header |
                     form | cookie field, or path regex with one capture group)
  objects.target     the authorised object (templated, e.g. "{item}"); objects.target_var: the variable the action
                     uses for the object (lets the self-test aim the same action at a decoy)
  objects.decoys     objects the self-test writes to on purpose (templated; captured by setup_per_run)
  objects.routes     optional extra watched write routes in the family [{name, method, path_regex}]
  objects.oracle     independent per-object SNAPSHOT of the whole family: {kind: exec, ...} printing "object|count" (or
                     tab-separated) lines, or {kind: http, ..., list, key, value?} (value absent: count items per key);
                     mode items|decrease. Effects per object = the change between the scenario-start and final snapshots
v1.2 (afd-workflow-contract/1.2, protocol contract-v2) = v1.1 plus application-contract fields (M4):
  guarantees     DECLARED properties (observed ones are written only by `afd contract probe`, never here):
                 idempotency {mechanism: none|key_header|natural_key|state_precondition, scope: identical_request|intent,
                 header?, window_s?}; read_back {available, routes: [{method: GET, path_regex}], ui_paths?: [regex],
                 max_lag_s?}; accept_semantics: commit|accepted_async; in_flight_bound_s?; read_lag_s?
  fresh_attempt  "none" or at most 5 HTTP steps {method GET|POST|PUT|PATCH, path "/...", json|form?, headers?, expect}
                 that re-establish the action's preconditions; sent THROUGH the proxy inside the sandbox (PC-B, G8)
  objects.effect_id {from: json, pointer} | {from: header, name}; objects.error_code {pointer}: one allow-listed
                 response field each, for causal attribution and protection classes
v1.3 (afd-workflow-contract/1.3, protocol contract-v3) = v1.2 plus optional fault hooks (M5):
  fault_hooks    {"late_backend_commit": {"steps": [<= 5 HTTP steps {service, port?, method POST|PUT|PATCH, path "/...",
                 json|form?, headers?, expect}], "max_delay_s": N}}: how the HARNESS makes this application commit an
                 accepted write later (template variable {delay_s}). Executed directly against a declared service on
                 the sealed app network, never through the proxy; part of the approved contract bytes. Without it the
                 late_backend_commit conditions are NOT APPLICABLE for the app.
  oracles and setup HTTP steps may name a non-public `port` of a declared service (e.g. a fixture's internal port)
Safety rules (all versions): containerised apps inside the AFD sandbox only (no remote URLs), synthetic data, an
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
    elif c.get("contract") not in ("afd-workflow-contract/1", "afd-workflow-contract/1.1", "afd-workflow-contract/1.2", "afd-workflow-contract/1.3"): raise ContractError("unsupported contract version")
    if c.get("contract") in ("afd-workflow-contract/1.1", "afd-workflow-contract/1.2", "afd-workflow-contract/1.3"): _check_objects(c)
    elif "objects" in c: raise ContractError("an 'objects' section requires afd-workflow-contract/1.1 or later")
    if c.get("contract") in ("afd-workflow-contract/1.2", "afd-workflow-contract/1.3"): _check_v12(c)
    elif any(k in c for k in ("guarantees", "fresh_attempt")): raise ContractError("'guarantees' / 'fresh_attempt' require afd-workflow-contract/1.2 or later")
    if c.get("contract") == "afd-workflow-contract/1.3": _check_v13(c)
    elif "fault_hooks" in c: raise ContractError("'fault_hooks' require afd-workflow-contract/1.3")
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

OBJECT_ID_RX = r"[A-Za-z0-9@._:+-]{1,128}"     # object ids the proxy records verbatim; anything else is recorded as "unparseable"

def _check_objects(c):
    o = c.get("objects")
    if not isinstance(o, dict): raise ContractError("afd-workflow-contract/1.1 requires an 'objects' section")
    for k in ("id", "target", "target_var", "decoys", "oracle"):
        if k not in o: raise ContractError(f"objects.{k} is required")
    i = o["id"]
    if i.get("in") not in SCOPE_IN: raise ContractError(f"objects.id.in must be one of {SCOPE_IN}")
    if i["in"] == "path":
        if re.compile(i["regex"]).groups != 1: raise ContractError("objects.id.regex must have exactly one capture group")
    elif not i.get("field"): raise ContractError("objects.id.field is required")
    if not (isinstance(o["decoys"], list) and o["decoys"]): raise ContractError("objects.decoys must list at least one decoy object")
    for r in o.get("routes") or []: re.compile(r["path_regex"])
    q = o["oracle"]
    if q.get("kind") not in ("http", "exec"): raise ContractError("objects.oracle.kind must be http or exec")
    if q.get("mode", "items") not in ("items", "decrease"): raise ContractError("objects.oracle.mode must be items or decrease")
    if q["kind"] == "http" and not (q.get("list") is not None and q.get("key")): raise ContractError("objects.oracle (http) needs list and key pointers")

FRESH_METHODS = ("GET", "POST", "PUT", "PATCH")

def _check_v12(c):
    g = c.get("guarantees") or {}
    if set(g) - {"idempotency", "read_back", "accept_semantics", "in_flight_bound_s", "read_lag_s", "note"}: raise ContractError(f"unknown guarantees: {sorted(set(g) - {'idempotency', 'read_back', 'accept_semantics', 'in_flight_bound_s', 'read_lag_s', 'note'})}")
    i = g.get("idempotency") or {}
    if i and i.get("mechanism") not in ("none", "key_header", "natural_key", "state_precondition"): raise ContractError("guarantees.idempotency.mechanism invalid")
    if i and i.get("mechanism") != "none" and i.get("scope") not in ("identical_request", "intent"): raise ContractError("guarantees.idempotency.scope must be identical_request or intent")
    rb = g.get("read_back") or {}
    if rb:
        if not isinstance(rb.get("available"), bool): raise ContractError("guarantees.read_back.available must be true or false")
        for r in rb.get("routes", []):
            if str(r.get("method", "GET")).upper() != "GET": raise ContractError("read_back routes must be GET")
            re.compile(r["path_regex"])
        for x in rb.get("ui_paths", []): re.compile(x)
    if g.get("accept_semantics", "commit") not in ("commit", "accepted_async"): raise ContractError("guarantees.accept_semantics must be commit or accepted_async")
    for k in ("in_flight_bound_s", "read_lag_s"):
        if g.get(k) is not None and not (isinstance(g[k], (int, float)) and g[k] >= 0): raise ContractError(f"guarantees.{k} must be a non-negative number or null")
    fa = c.get("fresh_attempt", "none")
    if fa != "none":
        if not (isinstance(fa, list) and 1 <= len(fa) <= 5): raise ContractError("fresh_attempt must be \"none\" or 1-5 HTTP steps")
        for st in fa:
            if set(st) - {"method", "path", "json", "form", "headers", "expect"}: raise ContractError(f"fresh_attempt step keys not allowed: {sorted(set(st) - {'method', 'path', 'json', 'form', 'headers', 'expect'})} (HTTP steps only; no exec, no set)")
            if str(st.get("method", "")).upper() not in FRESH_METHODS: raise ContractError("fresh_attempt method must be GET, POST, PUT or PATCH")
            if not str(st.get("path", "")).startswith("/") or "://" in str(st.get("path")): raise ContractError("fresh_attempt path must be relative to the proxied app")
            if not (isinstance(st.get("expect"), list) and st["expect"]): raise ContractError("fresh_attempt steps must declare expected statuses")
    o = c["objects"]
    for k in ("effect_id", "error_code"):
        if k in o:
            f = o[k]
            if not (isinstance(f, dict) and ((f.get("from", "json") == "json" and isinstance(f.get("pointer"), str)) or (f.get("from") == "header" and f.get("name")))): raise ContractError(f"objects.{k} must be {{from: json, pointer}} or {{from: header, name}}")

def _check_v13(c):
    fh = c.get("fault_hooks") or {}
    if set(fh) - {"late_backend_commit"}: raise ContractError(f"unknown fault hooks: {sorted(set(fh) - {'late_backend_commit'})}")
    names = {s["name"] for s in c["services"]}
    for name, h in fh.items():
        st = h.get("steps")
        if not (isinstance(st, list) and 1 <= len(st) <= 5): raise ContractError(f"fault_hooks.{name}.steps must be 1-5 HTTP steps")
        if not (isinstance(h.get("max_delay_s"), (int, float)) and 0 < h["max_delay_s"] <= 600): raise ContractError(f"fault_hooks.{name}.max_delay_s must be in (0, 600]")
        for x in st:
            if set(x) - {"service", "port", "method", "path", "json", "form", "headers", "expect"}: raise ContractError("fault hook steps are HTTP steps only (no exec, no set)")
            if x.get("service") not in names: raise ContractError("fault hook steps must target a declared service")
            if str(x.get("method", "")).upper() not in ("POST", "PUT", "PATCH"): raise ContractError("fault hook method must be POST, PUT or PATCH")
            if not str(x.get("path", "")).startswith("/") or "://" in str(x.get("path")): raise ContractError("fault hook path must be relative")
            if not (isinstance(x.get("expect"), list) and x["expect"]): raise ContractError("fault hook steps must declare expected statuses")
            if x.get("port") is not None and not (isinstance(x["port"], int) and 0 < x["port"] < 65536): raise ContractError("fault hook port must be a port number")

def snapshot_from(o, doc=None, stdout=None):
    """Parse a per-object oracle reading into {object id: number}."""
    out = {}
    if o["kind"] == "exec":
        for line in (stdout or "").splitlines():
            m = re.fullmatch(r"\s*(\S+?)\s*[|\t]\s*(-?\d+)\s*", line)
            if m: out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2))
        return out
    for it in pointer(doc, o["list"]):
        k = str(pointer(it, o["key"]))
        out[k] = out.get(k, 0) + (int(pointer(it, o["value"])) if o.get("value") else 1)
    return out

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

def protocol_for(contract, base_protocol_path=None):
    """The generic protocol, with the contract's identity recorded (no change to its rules). contract-v0 for v0/v1
    contracts; contract-v1 (per-object accounting) for v1.1 contracts."""
    if base_protocol_path is None:
        v = contract.get("contract")
        base_protocol_path = ROOT / "protocols" / ("contract-v3.json" if v == "afd-workflow-contract/1.3" else "contract-v2.json" if v == "afd-workflow-contract/1.2"
                                                   else "contract-v1.json" if "objects" in contract else "contract-v0.json")
    p = copy.deepcopy(json.loads(Path(base_protocol_path).read_text()))
    p["workflow"] = {"environment": contract["name"], "task": contract["action"]["name"], "authorised_intent": f"exactly {contract['intent']['exactly']} effect(s)"}
    return p
