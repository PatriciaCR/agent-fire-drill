"""Deterministic scripted recovery policies, VERSION 2 (M4.1 correction). Same policies and request behaviour as v1
(afd/agents/scripted.py, kept unchanged for reproducing frozen results); the only behavioural difference is the
user-visible check used by scripted_verify_no_resubmit:

  v1 walked the check's JSON pointer with string keys only, so any array step (e.g. "/0/title") raised TypeError, which
     was caught as "not confirmed". On apps whose check indexes an array the policy could never confirm.
  v2 resolves pointers per RFC 6901 (array indices, ~0/~1 escapes, the empty pointer for the whole document) and
     records, for EVERY check, an explicit read-back outcome:
       attempted   a check request was sent (always true for a check step)
       lookup      found | missing (absent key, out-of-range index, no matching list element) | malformed_pointer |
                   no_json (non-JSON or empty body) | http_error (non-200)
       confirmed   the value was found AND satisfied the check (success_in, or changed from the baseline)
     A 200 response, or a lookup that ran, is NOT a confirmation unless the expected value was found.
The claim of scripted_verify_no_resubmit is success iff some check was confirmed.

Policies: scripted_blind_retry, scripted_trust_response, scripted_verify_no_resubmit, scripted_wrong_target,
scripted_redo_retry (see v1 for descriptions). Config: env AFD_AGENT_CONFIG (as v1). Prints RESULT:<json>, where the
result carries "scripted_policy_version": "v2" and a "readback" list."""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

VERSION = "v2"

class PointerError(Exception):
    def __init__(self, kind, detail): super().__init__(detail); self.kind = kind     # kind: missing | malformed_pointer

def resolve(doc, ptr):
    """RFC 6901 JSON pointer. Raises PointerError('malformed_pointer'|'missing', ...)."""
    if ptr in (None, ""): return doc
    if not isinstance(ptr, str) or not ptr.startswith("/"): raise PointerError("malformed_pointer", f"pointer must be empty or start with '/': {ptr!r}")
    cur = doc
    for raw in ptr.split("/")[1:]:
        if re.search(r"~(?![01])", raw): raise PointerError("malformed_pointer", f"bad escape in {raw!r}")
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part): raise PointerError("malformed_pointer" if part != "-" else "missing", f"array index expected, got {part!r}")
            i = int(part)
            if i >= len(cur): raise PointerError("missing", f"index {i} out of range ({len(cur)} items)")
            cur = cur[i]
        elif isinstance(cur, dict):
            if part not in cur: raise PointerError("missing", f"key {part!r} absent")
            cur = cur[part]
        else: raise PointerError("missing", f"cannot descend into {type(cur).__name__} with {part!r}")
    return cur

def select_item(doc, spec):
    """{list: pointer, match: {field, prefix?, equals?}} -> the first matching element (PointerError 'missing' if none)."""
    if not spec: return doc
    items = resolve(doc, spec["list"])
    if not isinstance(items, list): raise PointerError("missing", "select.list does not point to an array")
    m = spec["match"]
    for it in items:
        if not isinstance(it, dict): continue
        v = str(it.get(m["field"], ""))
        if v.startswith(m.get("prefix", "")) and ("equals" not in m or v == str(m["equals"])): return it
    raise PointerError("missing", "no list element matches the selector")

def evaluate_check(k, status, doc):
    """Pure: the read-back outcome of one check response. Never raises."""
    out = {"attempted": True, "http_status": status, "lookup": None, "confirmed": False}
    if status != 200: out["lookup"] = "http_error"; return out
    if doc is None: out["lookup"] = "no_json"; return out
    try: v = resolve(select_item(doc, k.get("select")), k["value"])
    except PointerError as e: out["lookup"] = e.kind; out["detail"] = str(e)[:160]; return out
    out["lookup"] = "found"
    out["confirmed"] = (v != k["baseline"]) if "baseline" in k else (v in (k.get("success_in") or []))
    return out

# ------------------------------------------------------------------------------------------------------------- agent
def main():
    C = json.loads(os.environ["AFD_AGENT_CONFIG"])
    OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    STEPS, READBACK = [], []
    def call(method, path, body=None, headers=None, form=None):
        if form is not None: data, ctype = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        else: data, ctype = (json.dumps(body).encode() if body is not None else None), "application/json"
        req = urllib.request.Request(C["base"] + path, data=data, method=method, headers={"Content-Type": ctype, **(headers or {})})
        try:
            with OPEN.open(req, timeout=60) as r: return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try: return e.code, json.loads(e.read() or b"null")
            except ValueError: return e.code, None
        except ValueError: return 200, None
        except OSError: return 0, None
    def step(action, fn, note=None):
        t0 = time.time(); status, doc = fn(); t1 = time.time()
        STEPS.append({"step": len(STEPS) + 1, "t_start": t0, "t_end": t1, "actions": [action], "results": [],
                      "evaluation_previous_goal": f"{list(action)[0]} -> HTTP {status}" + (f"; {note}" if note else ""), "screenshot": None})
        return status, doc
    def submit(a=None):
        a = a or C["action"]; return step({"submit": {"path": a["path"]}}, lambda: call(a["method"], a["path"], a.get("body"), a.get("headers"), a.get("form")))
    def confirmed():
        k = C["check"]
        st, doc = step({"check": {"path": k["path"]}}, lambda: call(k["method"], k["path"], headers=k.get("headers")))
        r = evaluate_check(k, st, doc); r["step"] = len(STEPS); READBACK.append(r)
        STEPS[-1]["evaluation_previous_goal"] += f"; lookup {r['lookup']}, confirmed {r['confirmed']}"
        return r["confirmed"]
    policy = C["policy"]
    if policy == "scripted_blind_retry":
        for _ in range(3):
            st, _ = submit()
            if 200 <= st < 300: break
        claim = 200 <= st < 300
    elif policy == "scripted_trust_response":
        st, _ = submit(); claim = 200 <= st < 300
    elif policy == "scripted_verify_no_resubmit":
        submit(); claim = False
        for i in range(4):
            if confirmed(): claim = True; break
            time.sleep(3)
    elif policy == "scripted_redo_retry":
        for k in range(3):
            if k:
                for f in C.get("fresh") or []:
                    step({"fresh_attempt": {"path": f["path"]}}, lambda f=f: call(f["method"], f["path"], f.get("body"), f.get("headers"), f.get("form")))
            st, _ = submit()
            if 200 <= st < 300: break
        claim = 200 <= st < 300
    elif policy == "scripted_wrong_target":
        st, _ = submit(); claim = 200 <= st < 300
        submit(C["wrong_action"])
    else:
        sys.exit(f"unknown policy {policy}")
    STEPS.append({"step": len(STEPS) + 1, "t_start": time.time(), "t_end": time.time(), "actions": [{"done": {"success": claim}}], "results": [],
                  "evaluation_previous_goal": "finished", "screenshot": None})
    print("RESULT:" + json.dumps({"error": None, "scripted_policy_version": VERSION, "readback": READBACK,
                                  "claim": {"is_done": True, "claimed_success": claim, "final_result": "confirmed" if claim else "could not confirm; reported failure"}, "steps": STEPS}))

if __name__ == "__main__":
    main()
