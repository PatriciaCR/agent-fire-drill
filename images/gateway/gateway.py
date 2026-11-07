"""Agent Fire Drill inference gateway (Agent Fire Drill code; Apache-2.0). Standard library only.

The ONLY component that can read the Vultr Serverless Inference key. Untrusted agent workers reach it with a per-job
bearer token; the gateway enforces the job's model allow-list, output-token cap, request size, and USD budget, forwards
only the request body to the upstream with the real key, and records usage from EVERY upstream response (including
truncated ones) to a per-job ledger. Client headers are never forwarded; the key is never logged or returned.

Paths (container): key file  AFD_KEY_FILE (default /run/secrets/vultr_key, raw key or KEY=VALUE line)
                   tokens    AFD_TOKEN_DIR (read-only): <sha256(token)>.json = {job_id, budget_usd, models, max_tokens}
                   ledger    AFD_LEDGER_DIR (writable): <job_id>.jsonl, one line per upstream call
Endpoint: POST /v1/chat/completions  (Authorization: Bearer <job token>);  GET /healthz"""
import hashlib, json, os, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("AFD_UPSTREAM_BASE", "https://api.vultrinference.com/v1").rstrip("/")
KEY_FILE = os.environ.get("AFD_KEY_FILE", "/run/secrets/vultr_key")
TOKEN_DIR = os.environ.get("AFD_TOKEN_DIR", "/run/afd/tokens")
LEDGER_DIR = os.environ.get("AFD_LEDGER_DIR", "/var/lib/afd/ledger")
MAX_BODY = int(os.environ.get("AFD_MAX_BODY", str(16 * 1024 * 1024)))
UPSTREAM_TIMEOUT = float(os.environ.get("AFD_UPSTREAM_TIMEOUT", "180"))
# USD per token (prompt, completion), from Vultr's published model list (2026-11-06)
PRICES = {"glm-5.3-flash": (0.10e-6, 0.35e-6), "mimo-v2.6-flash": (0.10e-6, 0.25e-6), "glm-5.3": (0.75e-6, 3.00e-6)}
_LOCK = threading.Lock()

def _load_key():
    raw = open(KEY_FILE).read().strip()
    for line in raw.splitlines():
        if "=" in line and not line.strip().startswith("#"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return raw
KEY = _load_key()
if not KEY: raise SystemExit("empty key file")

def _token(header):
    if not header.startswith("Bearer "): return None
    tok = header[7:].strip()
    path = os.path.join(TOKEN_DIR, hashlib.sha256(tok.encode()).hexdigest() + ".json")
    try:
        with open(path) as f: return json.load(f)
    except (FileNotFoundError, ValueError): return None

def _spent(job_id):
    p = os.path.join(LEDGER_DIR, f"{job_id}.jsonl")
    try:
        with open(p) as f: return sum(json.loads(l)["cost_usd"] for l in f if l.strip())
    except FileNotFoundError: return 0.0

def _record(job_id, rec):
    with _LOCK, open(os.path.join(LEDGER_DIR, f"{job_id}.jsonl"), "a") as f: f.write(json.dumps(rec) + "\n")

def _cost(model, prompt, completion):
    pi, po = PRICES[model]; return prompt * pi + completion * po

class Handler(BaseHTTPRequestHandler):
    server_version = "afd-gateway"; sys_version = ""
    def log_message(self, fmt, *args): pass  # never log requests (bodies or headers)

    def _send(self, status, obj=None, raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz": return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        t0 = time.time()
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"): return self._send(404, {"error": "not found"})
        tok = _token(self.headers.get("Authorization", ""))
        if tok is None: return self._send(401, {"error": "invalid or revoked job token"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY: return self._send(413, {"error": "request too large"})
        try: body = json.loads(self.rfile.read(n))
        except ValueError: return self._send(400, {"error": "invalid JSON"})
        model = body.get("model")
        if model not in tok.get("models", []) or model not in PRICES: return self._send(403, {"error": f"model not allowed: {model}"})
        if body.get("stream"): return self._send(400, {"error": "streaming not supported"})
        cap = int(tok.get("max_tokens", 8192))
        for k in ("max_tokens", "max_completion_tokens"):
            if k in body: body[k] = min(int(body[k]), cap)
        if "max_tokens" not in body and "max_completion_tokens" not in body: body["max_completion_tokens"] = cap
        out_cap = int(body.get("max_completion_tokens") or body.get("max_tokens"))
        est_prompt = max(1, n // 3)  # conservative: about 3 bytes per token, images count as text bytes
        worst = _cost(model, est_prompt, out_cap)
        spent = _spent(tok["job_id"])
        if spent + worst > float(tok["budget_usd"]):
            return self._send(402, {"error": "job budget exhausted", "spent_usd": round(spent, 6), "budget_usd": tok["budget_usd"]})
        req = urllib.request.Request(UPSTREAM + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "User-Agent": "agent-fire-drill-gateway/1"})
        rec = {"t": round(t0, 3), "model": model, "request_bytes": n}
        try:
            with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as r:
                status, data = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, data = e.code, e.read()
        except Exception as e:  # transport failure: may have been billed -> charge the worst case
            _record(tok["job_id"], {**rec, "status": None, "error": type(e).__name__, "prompt_tokens": est_prompt, "completion_tokens": out_cap,
                                    "cost_usd": round(worst, 8), "estimated_upper_bound": True, "seconds": round(time.time() - t0, 2)})
            return self._send(502, {"error": "upstream unreachable"})
        usage, finish = None, None
        try:
            j = json.loads(data); usage = j.get("usage"); finish = (j.get("choices") or [{}])[0].get("finish_reason")
        except Exception: pass
        if status == 200 and usage:
            p, c = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
            _record(tok["job_id"], {**rec, "status": 200, "finish_reason": finish, "prompt_tokens": p, "completion_tokens": c,
                                    "cost_usd": round(_cost(model, p, c), 8), "estimated_upper_bound": False, "seconds": round(time.time() - t0, 2)})
        elif status == 200:  # success without usage: cannot track exactly -> worst case
            _record(tok["job_id"], {**rec, "status": 200, "finish_reason": finish, "prompt_tokens": est_prompt, "completion_tokens": out_cap,
                                    "cost_usd": round(worst, 8), "estimated_upper_bound": True, "seconds": round(time.time() - t0, 2)})
        else:  # upstream error status: recorded at zero cost (not billed), kept for audit
            _record(tok["job_id"], {**rec, "status": status, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
                                    "estimated_upper_bound": False, "seconds": round(time.time() - t0, 2)})
        self._send(status, raw=data)

if __name__ == "__main__":
    os.makedirs(LEDGER_DIR, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("AFD_PORT", "8080"))), Handler).serve_forever()
