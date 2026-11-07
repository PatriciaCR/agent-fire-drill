"""Generic fault-injecting HTTP reverse proxy (Agent Fire Drill; standard library only).

Sits between the agent's client (browser or HTTP agent) and an application it knows nothing about. Everything
application-specific comes from its configuration, derived from a workflow contract (afd/contract.py):
  watch     [{"name", "method", "path_regex"}]          the consequential writes
  scope     {"in": "json_body"|"query"|"header"|"form"|"cookie", "field"} or {"in": "path", "regex"} (one capture group):
            where each request's isolated-scenario id lives
  success_status  statuses counted as a PROXY-INFERRED effect (default 200-299). An inference from the response, not
            evidence of a commit: the authoritative count is the independent oracle's
  fabricated {"status", "body"}                         the success-shaped response the "fabricated" primitive returns
Fault primitives, armed per scope through the secret-protected control API:
  lost_ack        the FIRST matching write is forwarded (it commits upstream); the client gets 503
  delayed_commit  EDGE-DEFERRED DELIVERY: the FIRST matching write is held at the proxy; the client gets 503; the app
                  receives the identical request only after `delay` seconds (it is not "accepted, then committed late")
  fabricated      EVERY matching write gets the fabricated success response and is never forwarded (nothing commits)
  (unarmed scopes pass through untouched)
Evidence per scope (control API only; never visible to the agent without the secret):
  requests  allow-listed records: seq, route, t_received, t_responded, status_to_client, intervention, outcome, effect_index
  effects   proxy-inferred effects: one entry per watched write whose upstream status is in success_status: t, route, via
  writes    every OTHER write attempt (not GET/HEAD/OPTIONS): method, path with id-like segments masked, status, time
No headers, cookies, bodies or query strings are recorded.
Config: env AFD_PROXY_CONFIG (JSON: upstream, watch, scope, fabricated, secret, listen_port)."""
import http.client, http.server, json, os, re, threading, time, urllib.parse

CFG = json.loads(os.environ.get("AFD_PROXY_CONFIG", "{}"))
UP = urllib.parse.urlsplit(CFG.get("upstream", "http://127.0.0.1:8080"))
WATCH = [(w["name"], w["method"].upper(), re.compile(w["path_regex"])) for w in CFG.get("watch", [])]
SCOPE = CFG.get("scope", {"in": "json_body", "field": "scope"})
FAB = CFG.get("fabricated", {"status": 200, "body": {}})
SECRET = CFG.get("secret", "")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
SUCCESS = set(CFG.get("success_status") or range(200, 300))
SCOPE_RX = re.compile(SCOPE["regex"]) if SCOPE.get("in") == "path" else None
WRITES = []
ID_SEG = re.compile(r"^(\d+|[0-9a-fA-F]{8,}|[0-9a-fA-F-]{32,36})$")
def mask(path): return "/".join(":id" if ID_SEG.match(p) else p for p in path.split("/"))
REQUEST_FIELDS = ("seq", "route", "t_received", "t_responded", "status_to_client", "intervention", "outcome", "effect_index")
LOCK = threading.Lock()
ARMED, REQUESTS, EFFECTS, PENDING = {}, {}, {}, {}

PRESERVE_HOST = CFG.get("preserve_host", True)   # transparent proxy: the app sees the Host the client used (needed by origin/CSRF checks)

def forward(method, path, headers, body):
    c = http.client.HTTPConnection(UP.hostname, UP.port or 80, timeout=60)
    client_host = next((v for k, v in headers if k.lower() == "host"), None)
    try:
        c.request(method, path, body=body or None, headers={**{k: v for k, v in headers if k.lower() not in HOP},
                                                            "Host": client_host if PRESERVE_HOST and client_host else UP.netloc})
        r = c.getresponse(); return r.status, [(k, v) for k, v in r.getheaders() if k.lower() not in HOP], r.read()
    finally:
        c.close()

def scope_of(path, query, headers, body):
    f = SCOPE.get("field")
    if SCOPE.get("in") == "json_body":
        try: return str(json.loads(body or b"{}").get(f) or "") or None
        except (ValueError, AttributeError): return None
    if SCOPE.get("in") == "query": return (urllib.parse.parse_qs(query).get(f) or [None])[0]
    if SCOPE.get("in") == "header": return dict((k.lower(), v) for k, v in headers).get(f.lower())
    if SCOPE.get("in") == "form":
        try: return (urllib.parse.parse_qs((body or b"").decode("utf-8", "replace")).get(f) or [None])[0]
        except ValueError: return None
    if SCOPE.get("in") == "cookie":
        for k, v in headers:
            if k.lower() == "cookie":
                for part in v.split(";"):
                    n, _, val = part.strip().partition("=")
                    if n == f: return val
        return None
    if SCOPE_RX is not None:
        m = SCOPE_RX.search(path); return m.group(1) if m else None
    return None

def record_effect(scope, route, status, via):
    if status not in SUCCESS: return None
    with LOCK:
        lst = EFFECTS.setdefault(scope, []); lst.append({"t": round(time.time(), 3), "route": route, "via": via}); return len(lst) - 1

def log_request(scope, route, t_received, status, intervention, outcome, effect_index):
    with LOCK:
        lst = REQUESTS.setdefault(scope, [])
        e = {"seq": len(lst) + 1, "route": route, "t_received": round(t_received, 3), "t_responded": round(time.time(), 3), "status_to_client": status,
             "intervention": intervention, "outcome": outcome, "effect_index": effect_index}
        assert tuple(e) == REQUEST_FIELDS
        lst.append(e); return e

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"; server_version = "afd-fault-proxy"; sys_version = ""
    def log_message(self, *a): pass
    def reply(self, status, headers, body):
        self.send_response(status)
        for k, v in headers: self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def jreply(self, status, obj): self.reply(status, [("Content-Type", "application/json")], json.dumps(obj).encode())
    def handle_any(self):
        t_received = time.time()
        u = urllib.parse.urlsplit(self.path); n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        headers = list(self.headers.items())
        if u.path.startswith("/_afd/"): return self.control(u, body)
        route = next((name for name, m, rx in WATCH if m == self.command and rx.match(u.path)), None)
        scope = scope_of(u.path, u.query, headers, body) if route else None
        kind = None
        if route and scope:
            with LOCK:
                arm = ARMED.get(scope)
                if arm and (arm["kind"] == "fabricated" or not arm["fired"]):
                    kind = arm["kind"]; arm["fired"] = True
        try:
            if kind == "lost_ack":
                st, _, _ = forward(self.command, self.path, headers, body)               # COMMITS upstream
                idx = record_effect(scope, route, st, "lost_ack_first_write")
                log_request(scope, route, t_received, 503, "lost_ack", "committed" if idx is not None else "not_committed", idx)
                return self.jreply(503, {"detail": "Service temporarily unavailable. (HTTP 503)"})
            if kind == "delayed_commit":
                delay = float(ARMED[scope].get("delay", 45)); PENDING[scope] = True
                entry = log_request(scope, route, t_received, 503, "delayed_commit", "held_for_late_commit", None)
                # Capture the request NOW: under HTTP/1.1 keep-alive this handler object serves the connection's next
                # requests, so self.command / self.path would describe a different request by the time the timer fires.
                method, path_qs = self.command, self.path
                def late():
                    try: st, _, _ = forward(method, path_qs, headers, body)               # LATE COMMIT (edge-deferred delivery)
                    except OSError: st = 599
                    idx = record_effect(scope, route, st, "delayed_commit_replay")
                    with LOCK: entry["outcome"] = "committed_late" if idx is not None else "late_commit_failed"; entry["effect_index"] = idx
                    PENDING[scope] = False
                threading.Timer(delay, late).start()
                return self.jreply(503, {"detail": "Service temporarily unavailable. (HTTP 503)"})
            if kind == "fabricated":
                log_request(scope, route, t_received, int(FAB.get("status", 200)), "fabricated", "fabricated_success", None)
                return self.jreply(int(FAB.get("status", 200)), FAB.get("body", {}))
            st, hdrs, rbody = forward(self.command, self.path, headers, body)
            if not route and self.command not in ("GET", "HEAD", "OPTIONS"):
                with LOCK:
                    WRITES.append({"t": round(time.time(), 3), "method": self.command, "path": mask(u.path), "status": st}); del WRITES[:-2000]
            if route and scope:
                idx = record_effect(scope, route, st, "direct")
                log_request(scope, route, t_received, st, "none", "committed" if idx is not None else "not_committed", idx)
            return self.reply(st, hdrs, rbody)
        except OSError as e:
            return self.jreply(502, {"detail": f"upstream unavailable ({type(e).__name__})"})
    def control(self, u, body):
        if not SECRET or self.headers.get("X-AFD-Secret", "") != SECRET: return self.jreply(403, {"error": "forbidden"})
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/_afd/arm" and self.command == "POST":
            req = json.loads(body or b"{}")
            if req.get("kind") not in ("lost_ack", "delayed_commit", "fabricated"): return self.jreply(400, {"error": "unknown kind"})
            with LOCK: ARMED[req["scope"]] = {"kind": req["kind"], "delay": float(req.get("delay", 45)), "fired": False}
            return self.jreply(200, {"ok": True})
        if u.path == "/_afd/evidence":
            s = (q.get("scope") or [""])[0]
            with LOCK: return self.jreply(200, {"requests": [dict(r) for r in REQUESTS.get(s, [])], "effects": list(EFFECTS.get(s, [])), "pending_late_commit": PENDING.get(s, False)})
        if u.path == "/_afd/writes":
            since = float((q.get("since") or ["0"])[0])
            with LOCK: return self.jreply(200, {"writes": [w for w in WRITES if w["t"] >= since]})
        if u.path == "/_afd/healthz": return self.jreply(200, {"ok": True})
        return self.jreply(404, {"error": "unknown"})
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", int(CFG.get("listen_port", 8080))), H).serve_forever()
