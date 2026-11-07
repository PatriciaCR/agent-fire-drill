"""Booking-form reproduction without any model: connect over CDP to the isolated browser and try three ways of selecting
the slot in BreakingWeb's native <select id="apt-slot"> (React-controlled), checking whether the submit button enables and
whether submission creates exactly one appointment (verified by the orchestrator from the backend)."""
import json, os, time
from playwright.sync_api import sync_playwright

BU_JS = """(el, wanted) => {   // the core of browser-use 0.13.14's SelectDropdownOption handler for native <select>
  for (const option of el.options) {
    if (option.text.trim().toLowerCase() === wanted.trim().toLowerCase() || option.value.toLowerCase() === wanted.toLowerCase()) {
      el.value = option.value; option.selected = true; el.selectedIndex = option.index;
      el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
      return {success: el.value === option.value, value: el.value};
    }
  }
  return {success: false, error: 'Option with text or value not found', options: [...el.options].map(o => o.text)};
}"""
R = {}
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp(os.environ["CDP"]); page = b.contexts[0].pages[0] if b.contexts[0].pages else b.contexts[0].new_page()
    def fresh(url):
        page.goto(url); page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Schedule New Appointment").click()
        prov = page.locator("select[aria-label='Select provider']")
        label = [o for o in prov.locator("option").all_inner_texts() if "pcp" in o.lower() or "primary" in o.lower()][0]
        prov.select_option(label=label); page.wait_for_selector("#apt-slot"); page.wait_for_timeout(500)
        opts = page.locator("#apt-slot option").all_inner_texts()
        return opts
    submit = lambda: page.locator("button[aria-label='Submit new appointment']")
    opts = fresh(os.environ["URL_A"]); R["slot_options"] = opts; R["element"] = page.eval_on_selector("#apt-slot", "e => ({tag: e.tagName, multiple: e.multiple})")
    target = opts[1]
    R["a_browseruse_js_exact_text"] = page.eval_on_selector("#apt-slot", BU_JS, target); page.wait_for_timeout(300)
    R["a_submit_enabled"] = submit().is_enabled()
    corrupted = target.replace("11/", "11/**").replace(", ", "**, **", 1)
    R["c_corrupted_text_used"] = corrupted
    R["c_browseruse_js_corrupted_text"] = page.eval_on_selector("#apt-slot", BU_JS, corrupted)
    if R["a_submit_enabled"]:
        page.eval_on_selector("#apt-slot", BU_JS, target); submit().click(); page.wait_for_timeout(1500)
        R["a_submitted"] = True
    opts = fresh(os.environ["URL_B"])
    page.locator("#apt-slot").select_option(label=target); page.wait_for_timeout(300)
    R["b_playwright_select_option_enabled"] = submit().is_enabled()
    if R["b_playwright_select_option_enabled"]:
        submit().click(); page.wait_for_timeout(1500)
        R["b_toast_text"] = page.locator("body").inner_text()[:2000].count("Appointment scheduled")
    print("REPRO:" + json.dumps(R))
