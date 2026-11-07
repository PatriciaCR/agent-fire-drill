"""Strict CDP driver for harness-scripted browser sessions (login bootstrap, probe traffic recording). Runs inside a
short-lived harness container from the worker image, connected to the browser's CDP endpoint only.
Strict: "fill" fails if its element is missing or cannot take focus (instead of typing into whatever has focus), and
the driver stops at the first error or unmet "wait". Step kinds: nav, wait, fill, select, click, submit, key, sleep,
dump, shot. Output: one line RESULT:<json> (per-step records; screenshots base64 under png_b64).
Derived from studies/compat/browser.py:DRIVER (unchanged there)."""
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
                    ok_ = await js(f"(()=>{{const e=document.querySelector({json.dumps(st[1])}); if(!e) return false; e.focus(); e.select && e.select(); return document.activeElement===e}})()")
                    if not ok_: raise RuntimeError("fill target missing or not focused")
                    await call("Input.insertText", text=st[2])
                elif k == "select": rec["selected"] = await js(f"(()=>{{const e=document.querySelector({json.dumps(st[1])}); if(!e) return false; Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,'value').set.call(e,{json.dumps(st[2])}); e.dispatchEvent(new Event('change',{{bubbles:true}})); return e.value}})()")
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
            if rec.get("error") or False in (rec.get("found"), rec.get("clicked"), rec.get("submitted"), rec.get("selected")): break
    print("RESULT:" + json.dumps(out))
asyncio.run(main())
'''
