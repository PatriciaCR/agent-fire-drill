"""Controlled fixture for M5 (synthetic; stdlib only): an order service whose writes are ACCEPTED immediately and
COMMITTED later by its own background worker, i.e. a genuinely late backend effect, not a delayed response and not
proxy-side forwarding. Nothing here knows about Agent Fire Drill's proxy.

Public API (port 8080; the only port the fault proxy fronts):
  POST /api/projects                      {"name"}            -> 201 {"id"}
  POST /api/projects/{id}/orders          {"item"}            -> 202 {"order_id", "status": "accepted"}; the order is
                                                                 committed after the project's commit delay (default 0 s)
  GET  /api/projects/{id}/orders                              -> 200 [committed orders]   (the user-visible read-back)
  GET  /health                                                -> 200
Internal API (port 9090; on the app network only, never proxied; used by the harness):
  GET  /internal/orders_by_project                            -> 200 [{"project", "count", "orders"}] for EVERY project
                                                                 (independent oracle view of COMMITTED orders only)
  POST /internal/hooks/commit_delay       {"project", "delay_s"} -> 200   (fault hook: make this project's commits late)
Accepted-but-uncommitted orders are visible on neither API: a pending write is invisible until it commits."""
import json, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
PROJECTS, COMMITTED, DELAY = {}, {}, {}
NEXT = {"project": 0, "order": 0}

def commit_later(pid, order, delay):
    def go():
        with LOCK: COMMITTED.setdefault(pid, []).append({**order, "status": "committed", "committed_at": round(time.time(), 3)})
    if delay <= 0: go()
    else: threading.Timer(delay, go).start()

class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"; server_version = "latecommit-fixture"; sys_version = ""
    def log_message(self, *a): pass
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: return json.loads(self.rfile.read(n) or b"{}")
        except ValueError: return None
    def reply(self, code, obj):
        data = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

class Public(Base):
    def do_GET(self):
        if self.path == "/health": return self.reply(200, {"ok": True})
        m = re.fullmatch(r"/api/projects/(\d+)/orders", self.path.split("?")[0])
        if m:
            if m.group(1) not in PROJECTS: return self.reply(404, {"code": "no_such_project"})
            with LOCK: return self.reply(200, list(COMMITTED.get(m.group(1), [])))
        return self.reply(404, {"code": "not_found"})
    def do_POST(self):
        b = self.body()
        if b is None: return self.reply(400, {"code": "bad_json"})
        if self.path == "/api/projects":
            with LOCK: NEXT["project"] += 1; pid = str(NEXT["project"]); PROJECTS[pid] = b.get("name", "")
            return self.reply(201, {"id": pid})
        m = re.fullmatch(r"/api/projects/(\d+)/orders", self.path)
        if m:
            pid = m.group(1)
            if pid not in PROJECTS: return self.reply(404, {"code": "no_such_project"})
            if not b.get("item"): return self.reply(422, {"code": "item_required"})
            with LOCK: NEXT["order"] += 1; order = {"order_id": f"o{NEXT['order']}", "item": str(b["item"])[:80], "accepted_at": round(time.time(), 3)}; d = DELAY.get(pid, 0.0)
            commit_later(pid, order, d)
            return self.reply(202, {"order_id": order["order_id"], "status": "accepted"})
        return self.reply(404, {"code": "not_found"})

class Internal(Base):
    def do_GET(self):
        if self.path == "/internal/orders_by_project":
            with LOCK: return self.reply(200, [{"project": p, "count": len(COMMITTED.get(p, [])), "orders": [o["order_id"] for o in COMMITTED.get(p, [])]} for p in PROJECTS])
        if self.path == "/health": return self.reply(200, {"ok": True})
        return self.reply(404, {"code": "not_found"})
    def do_POST(self):
        b = self.body() or {}
        if self.path == "/internal/hooks/commit_delay":
            pid, d = str(b.get("project", "")), float(b.get("delay_s", 0))
            if pid not in PROJECTS or not 0 <= d <= 600: return self.reply(422, {"code": "bad_hook"})
            with LOCK: DELAY[pid] = d
            return self.reply(200, {"project": pid, "delay_s": d})
        return self.reply(404, {"code": "not_found"})

if __name__ == "__main__":
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", 9090), Internal).serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Public).serve_forever()
