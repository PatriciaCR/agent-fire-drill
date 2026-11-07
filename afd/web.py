"""Agent Fire Drill web interface (standard library; server-rendered; one small optional script for live progress).

Runs as afd-web: no Docker access, no key, no outbound network (host firewall). It reads sealed evidence bundles, request
status files and the latest containment evidence, and can only drop a small, schema-checked smoke request into the spool,
behind an access code. Preset policies only: visitors never submit free text that reaches the agent.

  GET  /  /drill  /results[?kind=]  /containment  /method        pages
  GET  /drill/<request>                                            live progress (LIVE badge only here and on its own result)
  GET  /results/<bundle>[?live=<request>]                          verdict (qualification / engine check) or drill (smoke)
  GET  /results/<bundle>/<run>                                     four-lane replay
  GET  /results/<bundle>.tar.gz                                    the sealed bundle
  GET  /results/<bundle>/shot/<run>[/<step>]                       recorded screenshots
  GET  /api/requests/<request>.json                                request status (the same data as the progress page)
  GET  /containment.json  /static/<allow-listed file>  /healthz
  POST /smoke                                                      access code + preset + condition -> spool request"""
import hashlib, hmac, json, os, re, secrets, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from afd import bundle as B, config as C, qualify as Q  # noqa: E402
from afd.ui import model as M, render as V  # noqa: E402

ACCESS_FILE = Path(os.environ.get("AFD_WEB_ACCESS_FILE", "/etc/afd/web_access"))
BIND, PORT = os.environ.get("AFD_WEB_BIND", "127.0.0.1"), int(os.environ.get("AFD_WEB_PORT", "8090"))
BID_RE, RID_RE, RUN_RE = re.compile(r"^(full|mini|smoke)-\d{8}T\d{6}Z-[0-9a-f]{6}$"), re.compile(r"^[a-z0-9-]{8,64}$"), re.compile(r"^r\d{3}$")
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC = {"style.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
          **{f"fonts/{f}": "font/woff2" for f in ("IBMPlexSans-Regular-Latin1.woff2", "IBMPlexSans-SemiBold-Latin1.woff2", "IBMPlexMono-Regular-Latin1.woff2", "IBMPlexMono-SemiBold-Latin1.woff2")}}
CSP = "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; font-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
PRESET_NAMES = tuple(V.POLICIES)
_RATE, _RATE_LOCK = {}, threading.Lock()

def hash_code(code, salt=None, it=200_000):
    salt = salt or secrets.token_hex(16)
    return f"pbkdf2_sha256${it}${salt}${hashlib.pbkdf2_hmac('sha256', code.encode(), bytes.fromhex(salt), it).hex()}"

def check_code(code):
    try: alg, it, salt, want = ACCESS_FILE.read_text().strip().split("$")
    except (OSError, ValueError): return False
    return alg == "pbkdf2_sha256" and hmac.compare_digest(hash_code(code, salt, int(it)), f"{alg}${it}${salt}${want}")

def rate_ok(ip, limit=5, window=3600):
    now = time.time()
    with _RATE_LOCK:
        q = [t for t in _RATE.get(ip, []) if now - t < window]; ok = len(q) < limit
        if ok: q.append(now)
        _RATE[ip] = q
    return ok

# ---------------------------------------------------------------------------------------------------------- evidence I/O
def bundles():
    out = []
    if not C.RESULTS_DIR.is_dir(): return out
    for d in C.RESULTS_DIR.iterdir():
        if d.is_dir() and BID_RE.match(d.name) and (d / "summary.json").exists():
            try: out.append(json.loads((d / "summary.json").read_text()))
            except ValueError: pass
    return sorted(out, key=lambda s: s.get("finished_utc", ""), reverse=True)

def containment_evidence():
    for p in (C.ACCEPTANCE_DIR / "latest.json", C.VAR / "acceptance" / "containment.json"):
        if p.exists():
            try: return p, json.loads(p.read_text())
            except ValueError: return p, None
    return None, None

def cview():
    return M.containment_view(containment_evidence()[1])

def check_counts(d):
    v = M.containment_view(d)
    return {"mandatory": v["mandatory_total"], "mandatory_passed": v["mandatory_passed"], "skipped": v["skipped"], "informational": len(v["informational"])}

def counts_text(d):
    c = check_counts(d); t = f"{c['mandatory_passed']}/{c['mandatory']} mandatory checks passed"
    if c["skipped"]: t += f" ({c['skipped']} skipped)"
    if c["informational"]: t += f" · {c['informational']} informational"
    return t

def request_status(rid):
    p = C.RESULTS_DIR / "_requests" / f"{rid}.json"
    try: return json.loads(p.read_text())
    except (OSError, ValueError):
        return {"id": rid, "state": "queued"} if (C.SPOOL_DIR / f"{rid}.json").exists() else None

def load_bundle(bid):
    d = C.RESULTS_DIR / bid
    s = json.loads((d / "summary.json").read_text())
    runs = [json.loads(l) for l in (d / "runs.jsonl").read_text().splitlines() if l.strip()] if (d / "runs.jsonl").exists() else []
    plan = json.loads((d / "plan.json").read_text()) if (d / "plan.json").exists() else {"runs": [], "conditions": []}
    try: protocol = json.loads((d / "protocol.json").read_text())
    except (OSError, ValueError): protocol = Q.load_protocol()[0]
    return d, s, runs, plan, protocol

def presets(): return {k: Q.policy_text(k) for k in PRESET_NAMES}

def pending_request():
    d = C.RESULTS_DIR / "_requests"
    if C.SPOOL_DIR.is_dir() and any(p.suffix == ".json" for p in C.SPOOL_DIR.iterdir()): return True
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                if json.loads(p.read_text()).get("state") in ("accepted", "running") and time.time() - p.stat().st_mtime < 3600: return True
            except (OSError, ValueError): pass
    return False

def result_json(d, run_id):
    try: return json.loads((d / "runs" / run_id / "result.json").read_text())
    except (OSError, ValueError): return {}

class H(BaseHTTPRequestHandler):
    server_version = "afd-web"; sys_version = ""
    def log_message(self, fmt, *a): sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0][:120]))
    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None, cache="no-store"):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Cache-Control", cache); self.send_header("Referrer-Policy", "no-referrer")
        if ctype.startswith("text/html"): self.send_header("Content-Security-Policy", CSP); self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def redirect(self, loc, code=303): self.send(code, b"", extra={"Location": loc})
    def not_found(self): self.send(404, V.simple("Not found", "There is nothing at this address.", cview()))
    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")   # set by the local reverse proxy only (we listen on loopback)
        return (fwd.split(",")[-1].strip() if fwd else self.client_address[0])[:64]

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path); path, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path == "/healthz": return self.send(200, b'{"ok":true}', "application/json")
            if path == "/": return self.send(200, V.landing(bundles(), cview()))
            if path == "/drill": return self.send(200, V.drill_form(cview(), presets=presets()))
            if path == "/method": return self.send(200, V.method(Q.load_protocol()[0], presets(), cview()))
            if path == "/presets": return self.redirect("/method", 301)
            if path == "/results":
                kind = (q.get("kind") or [None])[0]
                return self.send(200, V.results_list(bundles(), cview(), kind if kind in ("qualification", "drill", "engine") else None))
            if path == "/containment": return self.send(200, V.containment(cview()))
            if path == "/containment.json":
                p, _ = containment_evidence()
                return self.send(200, p.read_bytes(), "application/json") if p else self.not_found()
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name in STATIC: return self.send(200, (STATIC_DIR / name).read_bytes(), STATIC[name], cache="public, max-age=3600")
                return self.not_found()
            m = re.match(r"^/api/requests/([^/]+)\.json$", path)
            if m and RID_RE.match(m.group(1)):
                st = request_status(m.group(1))
                if not st: return self.send(404, b'{"error":"unknown request"}', "application/json")
                stage, failed = V.stage_of(st)
                out = {"state": st.get("state"), "stage": stage, "failed": failed, "accepted_at": st.get("accepted_at"),
                       "bundle": st.get("bundle") if st.get("state") == "done" else None,
                       "detail": (st.get("progress") or {}).get("state") or st.get("reason") or st.get("state")}
                return self.send(200, json.dumps(out).encode(), "application/json")
            m = re.match(r"^/(drill|requests)/([^/]+)$", path)
            if m and RID_RE.match(m.group(2)):
                if m.group(1) == "requests": return self.redirect(f"/drill/{m.group(2)}", 301)
                st = request_status(m.group(2))
                return self.send(200, V.drill_progress(m.group(2), st, cview())) if st else self.not_found()
            m = re.match(r"^/results/([^/]+?)(\.tar\.gz)?$", path)
            if m and BID_RE.match(m.group(1)) and (C.RESULTS_DIR / m.group(1) / "summary.json").exists():
                bid = m.group(1)
                if m.group(2):
                    f = C.RESULTS_DIR / f"{bid}.tar.gz"
                    return self.send(200, f.read_bytes(), "application/gzip", {"Content-Disposition": f"attachment; filename={bid}.tar.gz"}) if f.exists() else self.not_found()
                d, s, runs, plan, protocol = load_bundle(bid); ver = B.verify(d)
                if s.get("profile") == "smoke" and runs:
                    rid = (q.get("live") or [""])[0]
                    live = M.provenance(s, request_status(rid) if RID_RE.match(rid) else None)
                    return self.send(200, V.replay(bid, s, runs[0], result_json(d, runs[0]["run_id"]), ver, live, cview(), protocol))
                return self.send(200, V.verdict_page(bid, s, runs, plan, ver, cview(), protocol))
            m = re.match(r"^/results/([^/]+)/(r\d{3})$", path)
            if m and BID_RE.match(m.group(1)) and (C.RESULTS_DIR / m.group(1) / "summary.json").exists():
                d, s, runs, plan, protocol = load_bundle(m.group(1))
                run = next((r for r in runs if r.get("run_id") == m.group(2)), None)
                if run: return self.send(200, V.replay(m.group(1), s, run, result_json(d, run["run_id"]), B.verify(d), M.provenance(s), cview(), protocol))
            m = re.match(r"^/results/([^/]+)/shot/([^/]+?)(?:/(\d{1,3}))?$", path)
            if m and BID_RE.match(m.group(1)) and RUN_RE.match(m.group(2)):
                sd = C.RESULTS_DIR / m.group(1) / "runs" / m.group(2) / "screenshots"
                f = sd / f"step{int(m.group(3)):02d}.png" if m.group(3) else (sorted(sd.glob("step*.png")) or [None])[-1]
                if f and f.is_file(): return self.send(200, f.read_bytes(), "image/png", cache="public, max-age=86400")
            return self.not_found()
        except Exception as e:
            sys.stderr.write(f"error {type(e).__name__}: {e}\n"); return self.send(500, V.simple("Something went wrong", "The page could not be rendered.", None))

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/smoke": return self.not_found()
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 4096: return self.send(413, V.simple("Request too large", "The form submission was too large.", cview()))
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host", ""):
            return self.send(403, V.simple("Cross-site request refused", "Start drills from this site's own form.", cview()))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
        if not rate_ok(self.client_ip()): return self.send(429, V.drill_form(cview(), "Too many attempts from your address. Try again later.", presets()))
        if not check_code(form.get("code", "")): return self.send(403, V.drill_form(cview(), "Access code not accepted.", presets()))
        pol, cond = form.get("policy"), form.get("condition")
        if pol not in PRESET_NAMES or cond not in V.CONDITIONS: return self.send(400, V.drill_form(cview(), "Choose a preset policy and a condition.", presets()))
        if pending_request(): return self.send(409, V.drill_form(cview(), "A drill is already running. Only one runs at a time; try again in a few minutes.", presets()))
        rid = f"s-{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
        C.SPOOL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = C.SPOOL_DIR / f".{rid}.tmp"; tmp.write_text(json.dumps({"id": rid, "kind": "smoke", "policy": pol, "condition": cond, "t": time.time()}))
        os.chmod(tmp, 0o660); tmp.replace(C.SPOOL_DIR / f"{rid}.json")
        self.redirect(f"/drill/{rid}")

def main():
    print(f"afd-web on http://{BIND}:{PORT} (results {C.RESULTS_DIR}, spool {C.SPOOL_DIR})", flush=True)
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()

if __name__ == "__main__":
    os.umask(0o007); main()
