"""Workflow contracts: the minimum a developer supplies so Agent Fire Drill can drill a workflow it was not built for.
Contracts are DATA (contracts/*.json). This module and afd/generic.py contain no application-specific identifiers; a
test (tests/test_contract.py) enforces that.

Contract v0 (afd-workflow-contract/0):
  authorization  {owner, attestation, synthetic_data: true, runs_in_sandbox: true}
                 v0 only runs an app the developer ships as a container image, inside AFD's sealed networks. Remote URLs are
                 refused, so a contract can never point the fault injector or an agent at a third-party site.
  app            {image, command, port, mounts: [{src (repo-relative, read-only), dst}], uid, health_path}
  scenario       {method, path, body}: creates fresh, isolated state; the JSON response contains the scope id
  scope          {response_field}: where the scenario response carries the scope id
                 plus {in: json_body|query|header, field}: where every request of the action carries it
  item           {from_scenario: field} or {method, path, list, match: {field, prefix}, id}: the object acted on
  action         {name, method, path, body, path_regex}: the consequential write (templated with {scope}, {item})
  user_check     {method, path, select?, value, success_in? | changed_from_baseline?}: what an agent may look at to
                 confirm the outcome (it is part of the app's normal user-facing surface)
  effects        {method, path, select?, mode: items|decrease, value}: verifier-only count of committed effects (the
                 backend truth; never exposed to the agent)
  intent         {exactly: N}: the authorised number of effects
  fabricated     {status, body}: what the fabricated-success primitive returns"""
import copy, json, re, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("contract", "name", "authorization", "app", "scenario", "scope", "item", "action", "user_check", "effects", "intent", "fabricated")
_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

class ContractError(ValueError): pass

def load(path):
    c = json.loads(Path(path).read_text())
    missing = [k for k in REQUIRED if k not in c]
    if missing: raise ContractError(f"missing fields: {missing}")
    if c["contract"] != "afd-workflow-contract/0": raise ContractError("unsupported contract version")
    a = c["authorization"]
    if not (a.get("owner") and a.get("attestation") and a.get("synthetic_data") is True and a.get("runs_in_sandbox") is True):
        raise ContractError("authorization must name an owner, attest authorization, use synthetic data, and run inside the AFD sandbox")
    if any(k in c["app"] for k in ("url", "base_url", "host")): raise ContractError("v0 runs only containerised apps inside the sandbox; remote URLs are refused")
    for m in c["app"].get("mounts", []):
        src = (ROOT / m["src"]).resolve()
        if ROOT not in src.parents and src != ROOT: raise ContractError(f"mount outside the repository: {m['src']}")
    re.compile(c["action"]["path_regex"])
    if c["scope"].get("in") not in ("json_body", "query", "header"): raise ContractError("scope.in must be json_body, query or header")
    if c["effects"].get("mode") not in ("items", "decrease"): raise ContractError("effects.mode must be items or decrease")
    if not isinstance(c["intent"].get("exactly"), int): raise ContractError("intent.exactly must be an integer")
    return c

def render(obj, **vars_):
    """Replace {scope} / {item} in every string of a JSON-like structure."""
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

def http_json(base, method, path, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with _NOPROXY.open(req, timeout=timeout) as r: return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw or b"null")
        except ValueError: return e.code, None

class Scenario:
    """One isolated scenario of a contract's app: scope, item, and the backend truth (effect count) about it."""
    def __init__(self, contract, app_base):
        self.c, self.base = contract, app_base
        st, resp = http_json(app_base, contract["scenario"]["method"], contract["scenario"]["path"], contract["scenario"].get("body"))
        if st >= 300: raise ContractError(f"scenario setup failed: HTTP {st}")
        self.scope = str(pointer(resp, "/" + contract["scope"]["response_field"]))
        it = contract["item"]
        if "from_scenario" in it: self.item = str(pointer(resp, "/" + it["from_scenario"]))
        else:
            st, doc = http_json(app_base, it["method"], render(it["path"], scope=self.scope))
            self.item = str(select(doc, it)[it["id"]])
        self.baseline = self._raw_effect_value()

    def _raw_effect_value(self):
        e = self.c["effects"]
        st, doc = http_json(self.base, e["method"], render(e["path"], scope=self.scope, item=self.item))
        if st >= 300: raise ContractError(f"effect verification failed: HTTP {st}")
        v = pointer(select(doc, render(e.get("select"), scope=self.scope, item=self.item)), e["value"])
        return len(v) if e["mode"] == "items" else v

    def effects(self):
        """Committed effects since the scenario was created, from the app's own verifier endpoint (backend truth)."""
        v = self._raw_effect_value()
        return v - self.baseline if self.c["effects"]["mode"] == "items" else self.baseline - v

    def action_request(self):
        a = self.c["action"]
        return {"method": a["method"], "path": render(a["path"], scope=self.scope, item=self.item), "body": render(a.get("body"), scope=self.scope, item=self.item),
                "headers": {self.c["scope"]["field"]: self.scope} if self.c["scope"]["in"] == "header" else {}}

    def check_request(self):
        u = self.c["user_check"]
        out = {"method": u["method"], "path": render(u["path"], scope=self.scope, item=self.item), "select": render(u.get("select"), scope=self.scope, item=self.item),
               "value": u["value"], "success_in": u.get("success_in")}
        if u.get("changed_from_baseline"):
            st, doc = http_json(self.base, u["method"], out["path"])
            out["baseline"] = pointer(select(doc, out["select"]), u["value"])
        return out

def protocol_for(contract, base_protocol_path=ROOT / "protocols" / "contract-v0.json"):
    """The generic protocol, with the contract's identity recorded (no change to its rules)."""
    p = json.loads(Path(base_protocol_path).read_text())
    p = copy.deepcopy(p); p["workflow"] = {"environment": contract["name"], "task": contract["action"]["name"], "authorised_intent": f"exactly {contract['intent']['exactly']} effect(s)"}
    return p
