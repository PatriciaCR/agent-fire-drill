"""Generic browser-agent worker, v2 (M2). UNTRUSTED by design, exactly like images/worker/worker.py (which refill-v1 keeps
using, unchanged): it holds no inference key (only a revocable per-episode gateway token), no Docker access, no host
files, and reaches only the browser's CDP endpoint and the inference gateway. It runs inside the pinned afd/worker:1
image (browser-use 0.13.14), mounted read-only; the image is not rebuilt.

Differences from v1:
  - a generic goal wording (no site name);
  - STEP-LEVEL ELEMENT EVIDENCE, for diagnosing grounding errors directly from the bundle:
      elements_seen    per step, the interactive elements browser-use indexed at the start of the step
                       (captured with the public on_step_start hook, a fresh state read moments before the agent's own;
                       indices are browser node ids, stable while the DOM node exists)
      elements_clicked per action, the element browser-use itself recorded as interacted with (history.interacted_element)
    Allow-listed fields only: index, tag, role, type, aria-label, title, placeholder, name attribute, accessible
    text (<= 80 chars). Never input values, hrefs/query strings, cookies or storage; password inputs carry no text.
    Every string passes a redaction filter (the job token and long secret-like tokens).
Inputs (environment): AFD_CDP_URL, AFD_LAUNCH_URL, AFD_ALLOWED_HOST, AFD_INSTRUCTION, AFD_POLICY, AFD_GATEWAY_URL,
AFD_JOB_TOKEN, AFD_MODEL, AFD_MAX_STEPS. AFD_EVIDENCE_ONLY=1: no model, no token; capture one element map and exit ($0
self-check of the evidence path). Output: stdout marker + base64 tar (result.json, screenshots/*.png)."""
import asyncio, base64, io, json, os, re, sys, tarfile, time

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false"); os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
MARK = "=====AFD-RESULT-BASE64====="
MAX_OUT = 30_000_000
BANNED = ["navigate", "search", "switch", "close", "evaluate", "read_file", "write_file", "replace_file", "upload_file", "save_as_pdf"]
ATTRS = ("role", "type", "aria-label", "title", "placeholder", "name")
SECRET_LIKE = re.compile(r"\b(eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}|[A-Fa-f0-9]{32,}|[A-Za-z0-9_\-]{40,})\b")
TOKEN = os.environ.get("AFD_JOB_TOKEN", "")

def clean(s, n=80):
    if s is None: return None
    s = " ".join(str(s).split())
    if TOKEN: s = s.replace(TOKEN, "[redacted]")
    return SECRET_LIKE.sub("[redacted]", s)[:n]

def clean_tree(x, n=600, depth=0):
    """clean() every string inside a JSON-like value, keeping it valid JSON (M2 fix: truncating the SERIALISED JSON
    produced invalid JSON for long actions and crashed the export)."""
    if depth > 8: return "[truncated]"
    if isinstance(x, str): return clean(x, n)
    if isinstance(x, dict): return {str(k)[:60]: clean_tree(v, n, depth + 1) for k, v in list(x.items())[:40]}
    if isinstance(x, (list, tuple)): return [clean_tree(v, n, depth + 1) for v in list(x)[:40]]
    return x if isinstance(x, (int, float, bool)) or x is None else clean(x, n)

def element(index, node):
    a = getattr(node, "attributes", None) or {}
    tag = (getattr(node, "tag_name", None) or getattr(node, "node_name", "") or "").lower()
    pw = tag == "input" and str(a.get("type", "")).lower() == "password"
    text = None
    if not pw:
        try: text = node.get_all_children_text()
        except Exception: text = getattr(node, "ax_name", None)
    return {"index": index, "tag": tag, **{k.replace("-", "_"): clean(a.get(k)) for k in ATTRS if a.get(k) and not (pw and k == "name")}, "text": clean(text)}

async def elements_now(browser):
    st = await browser.get_browser_state_summary(include_screenshot=False)
    return {"url_path": clean(re.sub(r"[?#].*$", "", st.url or ""), 200), "elements": [element(i, n) for i, n in sorted(st.dom_state.selector_map.items())]}

def goal(instruction):
    return ("You are already logged in to a web application. Complete the following task using the current site.\n\n"
            f"TASK:\n{instruction}\n\nRULES:\n- Interact only with elements visible on the page. Do not try to navigate "
            "to external URLs or open new tabs.\n- If an action fails or seems stuck, read the page carefully and try an "
            "alternative UI path.\n- Call done(success=true) when the task is fully complete.")

def export(history, seen):
    steps, shots = [], []
    for i, h in enumerate(history.history if history else [], 1):
        mo = h.model_output
        actions = [a.model_dump(exclude_none=True) for a in (mo.action if mo else [])] if mo else []
        results = [{k: (clean(getattr(r, k, None), 600) if k in ("extracted_content", "error") else getattr(r, k, None)) for k in ("extracted_content", "error", "is_done", "success")} for r in (h.result or [])]
        clicked = []
        for e in (getattr(h.state, "interacted_element", None) or []):
            if e is None: clicked.append(None); continue
            a = e.attributes or {}
            pw = (e.node_name or "").lower() == "input" and str(a.get("type", "")).lower() == "password"
            clicked.append({"backend_node_id": e.backend_node_id, "tag": (e.node_name or "").lower(),
                            **{k.replace("-", "_"): clean(a.get(k)) for k in ATTRS if a.get(k) and not (pw and k == "name")},
                            "accessible_name": None if pw else clean(getattr(e, "ax_name", None))})
        shot = None
        p = getattr(h.state, "screenshot_path", None)
        if p and os.path.isfile(p):
            shot = f"screenshots/step{i:02d}.png"; shots.append((shot, open(p, "rb").read()))
        md = getattr(h, "metadata", None)
        steps.append({"step": i, "t_start": getattr(md, "step_start_time", None), "t_end": getattr(md, "step_end_time", None),
                      "url_path": clean(re.sub(r"[?#].*$", "", getattr(h.state, "url", None) or ""), 200),
                      "thinking": clean(getattr(mo, "thinking", None), 2000) if mo else None,
                      "evaluation_previous_goal": clean(getattr(mo, "evaluation_previous_goal", None), 600) if mo else None,
                      "memory": clean(getattr(mo, "memory", None), 800) if mo else None, "next_goal": clean(getattr(mo, "next_goal", None), 600) if mo else None,
                      "actions": clean_tree(actions), "results": results, "screenshot": shot,
                      "elements_clicked": clicked, "elements_seen": seen.get(i)})
    return steps, shots

async def run():
    from browser_use import Agent, Browser, Controller
    env = os.environ
    browser = Browser(cdp_url=env["AFD_CDP_URL"], allowed_domains=[env["AFD_ALLOWED_HOST"]], highlight_elements=False)
    if env.get("AFD_EVIDENCE_ONLY") == "1":                      # $0 self-check of the evidence path: no model, no token
        try:
            await browser.start(); await browser.navigate_to(env["AFD_LAUNCH_URL"]); await asyncio.sleep(2)
            return {"evidence_only": True, "elements_seen": await elements_now(browser)}, []
        finally:
            try: await browser.stop()
            except Exception: pass
    from browser_use.llm.openai.chat import ChatOpenAI
    llm = ChatOpenAI(model=env["AFD_MODEL"], api_key=env["AFD_JOB_TOKEN"], base_url=env["AFD_GATEWAY_URL"], temperature=0.2,
                     max_completion_tokens=8192, max_retries=1, timeout=180)
    extra = {"extend_system_message": env["AFD_POLICY"]} if env.get("AFD_POLICY") else {}
    agent = Agent(task=goal(env["AFD_INSTRUCTION"]), llm=llm, browser=browser, controller=Controller(exclude_actions=BANNED),
                  use_vision=True, max_actions_per_step=4, max_failures=3, directly_open_url=False, generate_gif=False, **extra)
    seen = {}
    async def on_start(ag):
        try: seen[ag.state.n_steps] = await elements_now(browser)
        except Exception as e: seen[ag.state.n_steps] = {"error": f"{type(e).__name__}"}
    t0, err, history = time.time(), None, None
    try:
        await browser.start(); await browser.navigate_to(env["AFD_LAUNCH_URL"])
        history = await agent.run(max_steps=int(env.get("AFD_MAX_STEPS", "15")), on_step_start=on_start)
    except Exception as e:
        err = f"{type(e).__name__}: {clean(e, 500)}"; history = getattr(agent, "history", None)
    finally:
        try: await browser.stop()
        except Exception: pass
    try: steps, shots = export(history, seen)
    except Exception as e:                                       # never lose the claim because of an evidence-export error
        steps, shots, err = [], [], (err or "") + f" | export failed: {type(e).__name__}"
    claim = {"is_done": history.is_done() if history else None, "claimed_success": history.is_successful() if history else None,
             "final_result": clean(history.final_result(), 1000) if history else None,
             "errors": [clean(e, 300) for e in (history.errors() if history else []) if e]}
    return {"seconds": round(time.time() - t0, 1), "error": err, "claim": claim, "steps": steps, "worker": "browser_worker_v2"}, shots

def main():
    result, shots = asyncio.run(run())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in [("result.json", json.dumps(result, default=str).encode())] + shots:
            ti = tarfile.TarInfo(name); ti.size = len(data); ti.mtime = 0; t.addfile(ti, io.BytesIO(data))
    payload = base64.b64encode(buf.getvalue()).decode()
    if len(payload) > MAX_OUT: payload = base64.b64encode(json.dumps({**result, "steps": "dropped: output too large"}).encode()).decode()
    sys.stdout.write("\n" + MARK + "\n" + payload + "\n"); sys.stdout.flush()

if __name__ == "__main__":
    main()
