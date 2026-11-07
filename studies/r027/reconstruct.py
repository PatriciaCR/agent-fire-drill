"""M0 r027 element-index reconstruction. No LLM: browser-use 0.13.14 (the pinned worker) builds its own DOM state and
selector map exactly as it does for the agent; clicks are dispatched as browser-use ClickElementEvents (what the agent's
click action does). Prints one line RECON:<json>."""
import asyncio, json, os, time

def card_of(node, depth=8):
    """The text of the nearest ancestor that looks like a medication card (contains 'mg')."""
    n = node
    for _ in range(depth):
        n = getattr(n, "parent_node", None)
        if n is None: return None
        t = " ".join(n.get_all_children_text().split())
        if "mg" in t: return t[:160]
    return None

def describe(node):
    a = node.attributes or {}
    return {"tag": node.tag_name, "text": " ".join(node.get_all_children_text().split())[:80], "aria": a.get("aria-label"), "id": a.get("id"),
            "data": {k: v for k, v in a.items() if k.startswith("data-")}, "backend_node_id": node.backend_node_id, "card": card_of(node)}

async def snapshot(b, label):
    st = await b.get_browser_state_summary(include_screenshot=False)
    sm = st.dom_state.selector_map
    return {"label": label, "t": round(time.time(), 2), "url": st.url, "n_interactive": len(sm), "indices": sorted(sm),
            "map": {str(i): describe(n) for i, n in sorted(sm.items())}, "llm_view": st.dom_state.llm_representation()[:6000]}

async def click(b, index):
    from browser_use.browser.events import ClickElementEvent
    node = await b.get_element_by_index(index)
    ev = b.event_bus.dispatch(ClickElementEvent(node=node)); await ev
    try: await ev.event_result(raise_if_any=True, raise_if_none=False)
    except Exception as e: return f"{type(e).__name__}: {str(e)[:200]}"
    return "ok"

async def main():
    from browser_use import Browser
    env = os.environ
    b = Browser(cdp_url=env["AFD_CDP_URL"], allowed_domains=[env["AFD_ALLOWED_HOST"]], highlight_elements=False)
    out = {"browser_use": __import__("importlib.metadata").metadata.version("browser-use"), "snapshots": [], "clicks": []}
    await b.start(); await b.navigate_to(env["AFD_LAUNCH_URL"]); await asyncio.sleep(2)
    out["snapshots"].append(await snapshot(b, "S1_initial (as at step 1)"))
    out["clicks"].append({"index": 15, "t": round(time.time(), 2), "result": await click(b, 15)})
    await asyncio.sleep(0.5); out["snapshots"].append(await snapshot(b, "S2_after_fabricated_0.5s (toast)"))
    await asyncio.sleep(5); out["snapshots"].append(await snapshot(b, "S3_after_fabricated_5s"))
    await asyncio.sleep(30); out["snapshots"].append(await snapshot(b, "S4_after_fabricated_35s (about step 3 timing)"))
    out["clicks"].append({"index": 17, "t": round(time.time(), 2), "result": await click(b, 17)})
    await asyncio.sleep(2); out["snapshots"].append(await snapshot(b, "S5_after_index_17_click"))
    try: await b.stop()
    except Exception: pass
    print("RECON:" + json.dumps(out, default=str), flush=True)

asyncio.run(main())
