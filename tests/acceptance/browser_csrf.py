"""Real-browser CSRF acceptance test (no inference, $0). Drives headless Chromium (afd/browser image) over CDP from the
worker image, against a local afd-web and a local attacker server, to check what real browsers send and what the app
accepts. Usage: python3 tests/acceptance/browser_csrf.py     Exit 0 only if every check passes.

Scenarios (all attacker pages use the CORRECT access code, so only the origin defence can stop them):
  diagnose        a page served with Referrer-Policy: no-referrer posts a form to its own origin; the echo endpoint
                  reports the Origin / Sec-Fetch-Site headers Chromium actually sent
  legitimate      the drill form on the app's own origin -> accepted (303 -> /drill/<id>)
  cross_site      attacker on another host -> refused
  same_site       attacker on the same host, another port (same site, different origin: like two *.sslip.io names) -> refused
  null_origin     attacker sandboxed iframe (Origin: null, cross-site) -> refused"""
import http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from afd.web import hash_code  # noqa: E402

APP_PORT, EVIL_PORT, CODE = 8095, 8096, "csrf-test-code"
DRIVER = r'''
import asyncio, json, sys, urllib.request, websockets
cdp, scen = sys.argv[1], json.loads(sys.argv[2])
async def main():
    tabs = json.load(urllib.request.urlopen(f"http://{cdp}/json/list"))
    ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"].replace("127.0.0.1:9223", cdp)
    async with websockets.connect(ws_url, max_size=2**24) as ws:
        n = [0]
        async def call(method, **params):
            n[0] += 1; i = n[0]; await ws.send(json.dumps({"id": i, "method": method, "params": params}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == i: return m.get("result", {})
        out = {}
        for name, steps in scen.items():
            for st in steps:
                if st[0] == "nav": await call("Page.navigate", url=st[1])
                elif st[0] == "js": await call("Runtime.evaluate", expression=st[1])
                elif st[0] == "wait": await asyncio.sleep(st[1])
            r = await call("Runtime.evaluate", expression="JSON.stringify({url: location.href, title: document.title, text: document.body ? document.body.innerText.slice(0, 300) : ''})", returnByValue=True)
            out[name] = json.loads(r["result"]["value"])
        print("RESULT:" + json.dumps(out))
asyncio.run(main())
'''

class Evil(http.server.BaseHTTPRequestHandler):
    target = None
    def log_message(self, *a): pass
    def page(self, body, extra=None):
        data = body.encode(); self.send_response(200); self.send_header("Content-Type", "text/html")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        form = (f"<form id=f method=post action='{self.target}/smoke'><input name=code value='{CODE}'>"
                "<input name=policy value=generic_guard><input name=condition value=clean></form>")
        if self.path == "/attack": return self.page(f"<title>attack</title>{form}<script>document.getElementById('f').submit()</script>")
        if self.path == "/attack-null":
            inner = form.replace("'", "&quot;") + "<script>document.getElementById(&quot;f&quot;).submit()</script>"
            return self.page(f"<title>attack-null</title><iframe sandbox='allow-forms allow-scripts' srcdoc='{inner}'></iframe>")
        if self.path == "/diag":
            return self.page("<title>diag</title><form id=f method=post action='/echo'><input name=x value=1></form><script>document.getElementById('f').submit()</script>",
                             {"Referrer-Policy": "no-referrer"})
    def do_POST(self):
        if self.path == "/echo":
            h = {k: self.headers.get(k) for k in ("Origin", "Sec-Fetch-Site", "Sec-Fetch-Mode", "Referer")}
            return self.page(f"<title>echo</title><pre>{json.dumps(h)}</pre>")

def sh(*a, **k): return subprocess.run(list(a), capture_output=True, text=True, **k)

def main():
    hosts = [l.split()[3].split("/")[0] for l in sh("ip", "-4", "-o", "addr", "show", "scope", "global").stdout.splitlines()]
    app_host = "172.17.0.1"; other = next(h for h in hosts if h not in (app_host,) and h.startswith("172."))
    tmp = tempfile.mkdtemp(prefix="afd-csrf-")
    for d in ("results", "spool", "acc"): os.makedirs(os.path.join(tmp, d))
    open(os.path.join(tmp, "access"), "w").write(hash_code(CODE))
    env = {**{k: v for k, v in os.environ.items() if "proxy" not in k.lower()}, "AFD_RESULTS": f"{tmp}/results", "AFD_SPOOL": f"{tmp}/spool",
           "AFD_ACCEPTANCE": f"{tmp}/acc", "AFD_WEB_ACCESS_FILE": f"{tmp}/access", "AFD_WEB_BIND": "0.0.0.0", "AFD_WEB_PORT": str(APP_PORT)}
    applog = open(os.path.join(tmp, "app.log"), "w")
    app = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "bin", "afd-web")], env=env, stdout=subprocess.DEVNULL, stderr=applog)
    refusals = lambda: open(os.path.join(tmp, "app.log")).read().count("csrf refused")
    Evil.target = f"http://{app_host}:{APP_PORT}"
    evil = http.server.ThreadingHTTPServer(("0.0.0.0", EVIL_PORT), Evil); threading.Thread(target=evil.serve_forever, daemon=True).start()
    browser = f"afd-csrf-browser-{os.getpid()}"
    results, ok = {}, True
    try:
        sh("docker", "run", "-d", "--name", browser, "--label", "afd.managed=1", "afd/browser:1")
        ip = sh("docker", "inspect", "-f", "{{(index .NetworkSettings.Networks \"bridge\").IPAddress}}", browser).stdout.strip()
        if not ip: raise RuntimeError("browser container has no bridge address")
        for _ in range(60):
            if sh("docker", "run", "--rm", "--entrypoint", "python", "afd/worker:1", "-c", f"import urllib.request;urllib.request.urlopen('http://{ip}:9222/json/version',timeout=2)").returncode == 0: break
            time.sleep(1)
        def clear_spool():
            for f in os.listdir(f"{tmp}/spool"): os.remove(os.path.join(tmp, "spool", f))
        def run(scen):
            r = sh("docker", "run", "--rm", "--entrypoint", "python", "afd/worker:1", "-c", DRIVER, f"{ip}:9222", json.dumps(scen), timeout=180)
            if "RESULT:" not in r.stdout: raise RuntimeError(r.stderr[-800:])
            return json.loads(r.stdout.split("RESULT:", 1)[1])
        def check(name, cond, detail):
            nonlocal ok; ok &= bool(cond); results[name] = {"pass": bool(cond), "detail": detail}
            print(f"{'PASS' if cond else 'FAIL'}  {name}  {json.dumps(detail)[:300]}", flush=True)
        d = run({"diagnose": [["nav", f"http://{app_host}:{EVIL_PORT}/diag"], ["wait", 2]]})["diagnose"]
        print("INFO  headers Chromium sends for a same-origin form POST under Referrer-Policy: no-referrer:", d["text"], flush=True)
        results["diagnose"] = {"pass": True, "detail": d["text"]}
        legit_js = ("document.getElementById('code').value=" + json.dumps(CODE) + ";document.querySelector('form[action=\"/smoke\"]').submit()")
        clear_spool()
        r = run({"legitimate": [["nav", f"http://{app_host}:{APP_PORT}/drill"], ["wait", 2], ["js", legit_js], ["wait", 2]]})["legitimate"]
        spooled = os.listdir(f"{tmp}/spool")
        check("legitimate_same_origin_submission_accepted", "/drill/s-" in r["url"] and len(spooled) == 1, {"url": r["url"], "title": r["title"], "spooled": spooled})
        for name, url in (("cross_site_attack_refused", f"http://{other}:{EVIL_PORT}/attack"), ("same_site_cross_origin_attack_refused", f"http://{app_host}:{EVIL_PORT}/attack"),
                          ("null_origin_sandboxed_iframe_attack_refused", f"http://{other}:{EVIL_PORT}/attack-null")):
            clear_spool(); before = refusals()
            r = run({name: [["nav", url], ["wait", 3]]})[name]
            spooled = os.listdir(f"{tmp}/spool"); reached = refusals() - before   # the attack must reach the app and be refused (no vacuous pass)
            check(name, not spooled and "/drill/s-" not in r["url"] and reached == 1, {"url": r["url"], "title": r["title"], "spooled": spooled, "refusals_logged": reached})
    finally:
        sh("docker", "rm", "-f", browser); app.terminate(); evil.shutdown(); shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(ROOT, "var", "acceptance"), exist_ok=True)
    json.dump({"at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pass": ok, "checks": results}, open(os.path.join(ROOT, "var", "acceptance", "browser_csrf.json"), "w"), indent=1)
    print("BROWSER CSRF:", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
