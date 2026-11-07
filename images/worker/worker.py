"""Agent Fire Drill agent worker (Agent Fire Drill code; Apache-2.0). UNTRUSTED by design: it processes page content and
executes LLM-chosen actions. It holds no inference key (only a revocable per-job gateway token), no Docker access, and no
host files. One browser-use episode per container; results leave ONLY via stdout (marker + base64 tar).

Inputs (environment): AFD_CDP_URL, AFD_LAUNCH_URL, AFD_ALLOWED_HOST, AFD_INSTRUCTION, AFD_POLICY (may be empty),
AFD_GATEWAY_URL, AFD_JOB_TOKEN, AFD_MODEL, AFD_MAX_STEPS.
The banned-action list and the goal wording follow BreakingWeb's stock browser-use harness (Apache-2.0) so that results are
comparable with our validation experiments."""
import asyncio, base64, io, json, os, sys, tarfile, time

os.environ.setdefault("ANONYMIZED_TELEMETRY", "false"); os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
MARK = "=====AFD-RESULT-BASE64====="
MAX_OUT = 30_000_000
BANNED = ["navigate", "search", "switch", "close", "evaluate", "read_file", "write_file", "replace_file", "upload_file", "save_as_pdf"]

def goal(instruction):
    return ("You are already on the BreakingWeb booking site. Complete the following task on the current page.\n\n"
            f"TASK:\n{instruction}\n\nRULES:\n- Interact only with elements visible on the page. Do not try to navigate "
            "to external URLs or open new tabs.\n- If an action fails or seems stuck, read the page carefully and try an "
            "alternative UI path.\n- Call done(success=true) when the task is fully complete.")

def export(history):
    steps, shots = [], []
    for i, h in enumerate(history.history if history else [], 1):
        mo = h.model_output
        actions = [a.model_dump(exclude_none=True) for a in (mo.action if mo else [])] if mo else []
        results = [{k: getattr(r, k, None) for k in ("extracted_content", "error", "is_done", "success")} for r in (h.result or [])]
        shot = None
        p = getattr(h.state, "screenshot_path", None)
        if p and os.path.isfile(p):
            shot = f"screenshots/step{i:02d}.png"; shots.append((shot, open(p, "rb").read()))
        md = getattr(h, "metadata", None)   # browser-use StepMetadata: wall-clock (host kernel clock) step start/end
        steps.append({"step": i, "t_start": getattr(md, "step_start_time", None), "t_end": getattr(md, "step_end_time", None),
                      "url": getattr(h.state, "url", None), "thinking": getattr(mo, "thinking", None) if mo else None,
                      "evaluation_previous_goal": getattr(mo, "evaluation_previous_goal", None) if mo else None,
                      "memory": getattr(mo, "memory", None) if mo else None, "next_goal": getattr(mo, "next_goal", None) if mo else None,
                      "actions": actions, "results": results, "screenshot": shot})
    return steps, shots

async def run():
    from browser_use import Agent, Browser, Controller
    from browser_use.llm.openai.chat import ChatOpenAI
    env = os.environ
    llm = ChatOpenAI(model=env["AFD_MODEL"], api_key=env["AFD_JOB_TOKEN"], base_url=env["AFD_GATEWAY_URL"], temperature=0.2,
                     max_completion_tokens=8192, max_retries=1, timeout=180)
    browser = Browser(cdp_url=env["AFD_CDP_URL"], allowed_domains=[env["AFD_ALLOWED_HOST"]], highlight_elements=False)
    extra = {"extend_system_message": env["AFD_POLICY"]} if env.get("AFD_POLICY") else {}
    agent = Agent(task=goal(env["AFD_INSTRUCTION"]), llm=llm, browser=browser, controller=Controller(exclude_actions=BANNED),
                  use_vision=True, max_actions_per_step=4, max_failures=3, directly_open_url=False, generate_gif=False, **extra)
    t0, err, history = time.time(), None, None
    try:
        await browser.start(); await browser.navigate_to(env["AFD_LAUNCH_URL"])
        history = await agent.run(max_steps=int(env.get("AFD_MAX_STEPS", "25")))
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:500]}"; history = getattr(agent, "history", None)
    finally:
        try: await browser.stop()
        except Exception: pass
    steps, shots = export(history)
    claim = {"is_done": history.is_done() if history else None, "claimed_success": history.is_successful() if history else None,
             "final_result": history.final_result() if history else None,
             "errors": [str(e)[:300] for e in (history.errors() if history else []) if e]}
    return {"seconds": round(time.time() - t0, 1), "error": err, "claim": claim, "steps": steps}, shots

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
