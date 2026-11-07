"""D6 real-browser check ($0, no LLM): a scripted Chromium (afd/browser image) on the agent network drives the app's
real UI THROUGH the fault proxy, with a fault armed, and the study records what the UI showed and what the independent
oracle saw. The driver is generic; per-app UI steps are DATA in studies/compat/apps/<app>.json ("browser": [...]).
Step kinds: nav <url>, wait <css> [timeout], fill <css> <text>, click <css>, submit <css> (the element's form, as a user's Enter does),
key <Enter>, sleep <s>, dump, shot <name>.
Usage: python3 studies/compat/browser.py <app> [condition]       (condition default: lost_ack)"""
import base64, json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from afd import config as C, contract as K, dockerops as D, firewall as F, gateway as G, generic as GEN  # noqa: E402

DRIVER = r'''
import asyncio, base64, json, sys, urllib.request, websockets
cdp, steps = sys.argv[1], json.loads(sys.argv[2])
async def main():
    for _ in range(60):
        try: tabs = json.load(urllib.request.urlopen(f"http://{cdp}/json/list")); break
        except Exception: await asyncio.sleep(0.5)
    ws_url = [t for t in tabs if t["type"] == "page"][0]["webSocketDebuggerUrl"].replace("127.0.0.1:9223", cdp)
    out = []
    async with websockets.connect(ws_url, max_size=2**25) as ws:
        n = [0]
        async def call(method, **params):
            n[0] += 1; i = n[0]; await ws.send(json.dumps({"id": i, "method": method, "params": params}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == i: return m.get("result", {})
        async def js(expr):
            r = await call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True); return (r.get("result") or {}).get("value")
        for st in steps:
            k = st[0]; rec = {"step": st}
            try:
                if k == "nav": await call("Page.navigate", url=st[1]); await asyncio.sleep(2)
                elif k == "wait":
                    t0 = asyncio.get_event_loop().time(); ok = False
                    while asyncio.get_event_loop().time() - t0 < (st[2] if len(st) > 2 else 20):
                        if await js(f"!!document.querySelector({json.dumps(st[1])})"): ok = True; break
                        await asyncio.sleep(0.3)
                    rec["found"] = ok
                elif k == "fill":
                    await js(f"(()=>{{const e=document.querySelector({json.dumps(st[1])}); e.focus(); e.select && e.select(); return true}})()")
                    await call("Input.insertText", text=st[2])
                elif k == "submit": rec["submitted"] = await js(f"(()=>{{const e=document.querySelector({json.dumps(st[1])}); if(!e||!e.form) return false; e.form.requestSubmit(); return true}})()")
                elif k == "click": rec["clicked"] = await js(f"(()=>{{const e=document.querySelector({json.dumps(st[1])}); if(!e) return false; e.click(); return true}})()")
                elif k == "key":
                    for t in ("keyDown", "keyUp"): await call("Input.dispatchKeyEvent", type=t, key=st[1], code=st[1], windowsVirtualKeyCode=13 if st[1] == "Enter" else 0)
                elif k == "sleep": await asyncio.sleep(st[1])
                elif k == "dump": rec["dump"] = await js("JSON.stringify({url: location.href, inputs: [...document.querySelectorAll('input,button,textarea,select')].slice(0,40).map(e => ({tag: e.tagName, id: e.id, name: e.name, type: e.type, ph: e.placeholder, text: (e.innerText||'').slice(0,40)}))})")
                elif k == "shot": rec["png_b64"] = (await call("Page.captureScreenshot", format="png"))["data"]
                rec["text"] = (await js("document.body ? document.body.innerText.slice(0, 600) : ''")) or ""
                rec["url"] = await js("location.href")
            except Exception as e: rec["error"] = f"{type(e).__name__}: {e}"
            out.append(rec)
    print("RESULT:" + json.dumps(out))
asyncio.run(main())
'''

def main(app, condition="lost_ack"):
    cfg = json.loads((ROOT / "studies" / "compat" / "apps" / f"{app}.json").read_text())
    c = K.load(ROOT / cfg["contract"]); res = {"app": app, "condition": condition, "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    F.install(); G.up()
    outdir = ROOT / "studies" / "compat" / "results" / f"{app}-browser"; outdir.mkdir(parents=True, exist_ok=True)
    with GEN.GenericJob(c, "browser") as job:
        s = GEN.Scenario(job); job.arm(s.scope, condition, 6); t_start = time.time()
        browser = f"afd-{job.id}-browser"
        D.sh("docker", "run", "-d", "--name", browser, "--network", job.nets["agent"], *D.labels(job.id, "browser"), *D.hardening("browser", C.BROWSER_UID), C.IMAGES["browser"])
        try:
            cdp = f"{D.ip_of(browser, job.nets['agent'])}:9222"
            steps = K.render(cfg["browser"], **s.vars)
            r = D.sh("docker", "run", "--rm", "--network", job.nets["agent"], *D.labels(job.id, "driver"), *D.hardening("worker", C.WORKER_UID), "--entrypoint", "python",
                     C.IMAGES["worker"], "-c", DRIVER, cdp, json.dumps(steps), check=False, timeout=300)
            recs = json.loads(r.stdout.split("RESULT:", 1)[1]) if "RESULT:" in r.stdout else [{"error": r.stderr[-800:]}]
        finally:
            D.remove_container(browser)
        series = []
        for _ in range(4 if condition == "delayed_commit" else 1):     # oracle readings over time (late commits)
            series.append({"t_after_start_s": round(time.time() - t_start, 1), "verified": s.effects()})
            if condition == "delayed_commit": time.sleep(5)
        ev = job.evidence(s.scope); n = s.effects()
        applog = D.sh("docker", "logs", job.containers[job.public["name"]], check=False)
        res["oracle_series"] = series
        rx = __import__("re").compile(c["action"]["path_regex"].strip("^$").replace("[^/]+", ".+").replace("[0-9]+", ".+"))
        res["app_log_writes"] = [l[-260:] for l in (applog.stdout + applog.stderr).splitlines() if rx.search(l) or "cross-origin" in l.lower() or "origin" in l.lower() and "denied" in l.lower()][-8:]
        res["proxy_effects"] = ev["effects"]
        for i, rec in enumerate(recs):
            if rec.get("png_b64"): (outdir / f"{condition}-{i:02d}.png").write_bytes(base64.b64decode(rec.pop("png_b64"))); rec["png"] = f"{condition}-{i:02d}.png"
        secrets = [str(job.vars[k]) for k in job.secret_vars] + [str(job.vars[k]) for k in job.captured_names if "token" in k or "cookie" in k or "session" in k]
        blob = json.dumps(recs)
        for sec in secrets:
            if sec and len(sec) > 4: blob = blob.replace(sec, "[redacted]")
        res.update(steps=json.loads(blob), proxy_requests=ev["requests"], verified=n, proxy_inferred=len(ev["effects"]),
                   other_writes=GEN.classify_writes(job.writes(t_start), c.get("supporting_writes")))
    (outdir / f"{condition}.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({**res, "steps": [{k: (v[:300] if isinstance(v, str) else v) for k, v in x.items()} for x in res["steps"]]}, indent=1)[:6000])

if __name__ == "__main__":
    main(sys.argv[1], *(sys.argv[2:3]))
