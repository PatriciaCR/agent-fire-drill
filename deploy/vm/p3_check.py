#!/usr/bin/env python3
"""Read-only verifier for a sealed `afd llm-episodes` bundle (P3 stages D and E). Changes nothing.
Usage: p3_check.py BUNDLE --stage preflight|rehearsal [--expect-mode rootless] [--ledger-dir DIR] [--token-dir DIR]
Manifest integrity is checked FIRST; then each item is printed as CONFIRMED or FAILED, and the exit status is 0 only
if every item holds:
  integrity, mode, preflight checks;
  worker network (from inside the worker): exactly CDP + gateway open; every required category closed; no DNS;
  browser network (from inside the browser + Chromium): only the proxy; every required category closed;
  gateway authorization: 402 / 403 / 401 / 401, token scope = the model and max_tokens, revoked;
  non-root confinement and cgroup limits for both containers (values from this commit's afd.config.LIMITS);
  provider key isolation;
  zero upstream model calls: from the bundle AND, with --ledger-dir, an independent re-read of the gateway ledger
    (every llm job ledger file written since the bundle started);
  teardown, and with --token-dir, no live token of any llm job;
  rehearsal only: both episodes budget_stop, 0 model calls, refusals > 0, gate passed in-episode, tokens revoked, $0."""
import argparse, json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from afd import bundle as B, config as C, generic_llm as GL  # noqa: E402

WORKER_CLOSED_REQUIRED = ("svc-", "proxy-agent-side:", "proxy-app-side:", "proxy-alias:", "metadata-", "internet-1.1.1.1", "internet-8.8.8.8", "internet-v6-",
                          "rootless-host-10.0.2.2", "rootless-dns-", "gateway-other-port:", "cdp-net-gateway:", "gw-net-gateway:", "vm-public-ip:")
BROWSER_CLOSED_REQUIRED = ("svc-", "proxy-app-side:", "inference-gateway", "metadata-", "internet-1.1.1.1", "internet-v6-", "rootless-host-10.0.2.2",
                           "agent-net-gateway:", "cdp-net-gateway:", "vm-public-ip:")
PREFLIGHT_CHECKS = ("P1_selftest_G1_G7", "P2_worker_isolation_in_container", "P3_browser_isolation_in_container", "P4_browser_reaches_app_only_via_proxy",
                    "P5_proxy_control_api_refuses_agent_side", "P6_gateway_token_caps_scope_revocation", "P7_provider_key_isolation", "P8_element_map_evidence",
                    "P9_lost_ack_intercepted_and_oracle_verified", "P10_positive_control_duplicate_detected", "teardown")

class Report:
    def __init__(self): self.items = []
    def item(self, name, ok, detail):
        self.items.append((name, bool(ok))); print(f"{'CONFIRMED' if ok else 'FAILED   '} {name}: {detail}", flush=True)
    @property
    def ok(self): return bool(self.items) and all(ok for _, ok in self.items)

def network(rep, label, probe, want_open, required):
    opened = {k for k, v in (probe.get("open") or {}).items() if v == "OPEN"}
    closed = probe.get("closed") or {}
    leaks = sorted(k for k, v in closed.items() if v == "OPEN")
    missing = [p for p in required if not any(k.startswith(p) for k in closed)]
    dns = sorted(n for n, v in (probe.get("dns") or {}).items() if v == "RESOLVED")
    rep.item(f"{label} network (from inside the {label} container)", opened == set(want_open) and set(probe.get("open") or {}) == set(want_open) and not leaks and not missing and not dns,
             f"open={sorted(opened)} (required exactly {sorted(want_open)}); {len(closed) - len(leaks)}/{len(closed)} closed; leaks={leaks}; missing categories={missing}; resolving names={dns}")

def confinement(rep, label, r, limits):
    conf = {k: r.get(k) for k in ("uid", "cap_eff", "no_new_privs", "seccomp", "rootfs")}
    ok = r.get("uid") not in (0, None) and r.get("cap_eff") and not str(r.get("cap_eff")).strip("0") and r.get("no_new_privs") == "1" and r.get("seccomp") == "2" and str(r.get("rootfs", "")).startswith("read-only")
    rep.item(f"{label} non-root confinement", ok, conf)
    got = {k: r.get(k) for k in limits}
    rep.item(f"{label} cgroup limits", all(str(got[k]) == str(v) for k, v in limits.items()), f"got {got}; expected {limits}")

def ledger_since(ledger_dir, t0):
    rows, files = [], []
    for p in sorted(Path(ledger_dir).glob("llm*.jsonl")):
        if p.stat().st_mtime >= t0:
            files.append(p.name)
            rows += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return files, rows

def main(argv=None):
    a = argparse.ArgumentParser(); a.add_argument("bundle"); a.add_argument("--stage", choices=("preflight", "rehearsal"), required=True)
    a.add_argument("--expect-mode", default="rootless"); a.add_argument("--ledger-dir"); a.add_argument("--token-dir")
    o = a.parse_args(argv); d = Path(o.bundle); rep = Report()
    v = B.verify(d)
    rep.item("bundle integrity (MANIFEST.sha256)", v["ok"], f"digest {v.get('digest')}, {v.get('files')} files, problems={v.get('problems')}")
    if not v["ok"]: print("P3 CHECK FAIL"); return 1
    j = lambda n: json.loads((d / n).read_text())
    summ, pre, env = j("summary.json"), j("preflight.json"), j("environment.json")
    runs = [json.loads(l) for l in (d / "runs.jsonl").read_text().splitlines() if l.strip()]
    rep.item("mode", summ.get("mode") == o.expect_mode == env.get("mode"), f"bundle {summ.get('mode')}, expected {o.expect_mode}")
    failed = [k for k in PREFLIGHT_CHECKS if not (pre.get(k) or {}).get("pass")]
    rep.item("preflight checks P1-P10 + teardown", summ.get("preflight_pass") is True and not failed and set(pre) == set(PREFLIGHT_CHECKS), f"failed={failed}; present={len(pre)}")
    w = pre.get("P2_worker_isolation_in_container") or {}
    b = (pre.get("P3_browser_isolation_in_container") or {}).get("result") or {}
    network(rep, "worker", w, {"browser-cdp", "inference-gateway"}, WORKER_CLOSED_REQUIRED)
    network(rep, "browser", b, {"proxy-alias:8080"}, BROWSER_CLOSED_REQUIRED)
    paths = (pre.get("P4_browser_reaches_app_only_via_proxy") or {}).get("paths") or {}
    rep.item("Chromium reaches the app only through the proxy", paths.get("via_proxy", {}).get("reached") is True and len(paths) >= 4 and not any(x.get("reached") for k, x in paths.items() if k != "via_proxy"),
             {k: x.get("landed") for k, x in paths.items()})
    g = pre.get("P6_gateway_token_caps_scope_revocation") or {}
    st, sc = g.get("statuses") or {}, g.get("token_scope") or {}
    rep.item("gateway authorization", st == {"budget_below_worst_case": 402, "model_not_allowed": 403, "bad_token": 401, "revoked_token": 401}
             and sc.get("models") == [summ.get("model")] and sc.get("max_tokens") == C.MAX_TOKENS and g.get("token_revoked") is True, f"statuses={st}; scope={sc}; revoked={g.get('token_revoked')}")
    confinement(rep, "worker", w, GL.expected_limits("worker"))
    confinement(rep, "browser", b, GL.expected_limits("browser"))
    k = pre.get("P7_provider_key_isolation") or {}
    rep.item("provider key isolation", k.get("pass") is True and k.get("orchestrator_can_read_key_file") is False and k.get("worker_run_secrets") is False and k.get("browser_mounts") == [],
             f"orchestrator can read key: {k.get('orchestrator_can_read_key_file')}; worker /run/secrets: {k.get('worker_run_secrets')}; browser mounts: {k.get('browser_mounts')}")
    led = g.get("ledger") or {}
    upstream = led.get("calls", 1) + sum((r.get("spend") or {}).get("calls", 1) for r in runs)
    cost = (led.get("cost_usd") or 0) + sum((r.get("spend") or {}).get("cost_usd", 1) for r in runs)
    rep.item("zero upstream model calls (bundle)", upstream == 0 and cost == 0 and summ.get("spent_total_usd") == 0, f"upstream calls={upstream}; cost=${cost}; summary spent=${summ.get('spent_total_usd')}")
    if o.ledger_dir:
        t0 = time.mktime(time.strptime(summ["bundle"].split("-")[1], "%Y%m%dT%H%M%SZ")) - time.timezone - 60
        files, rows = ledger_since(o.ledger_dir, t0)
        up = [r for r in rows if not r.get("refused")]
        rep.item("zero upstream model calls (gateway ledger, re-read independently)", files and not up and sum(r.get("cost_usd", 0) for r in rows) == 0,
                 f"{len(files)} llm ledger file(s) since the bundle started, {len(rows)} row(s), {len(up)} upstream, cost ${sum(r.get('cost_usd', 0) for r in rows)}")
    td = pre.get("teardown") or {}
    rep.item("teardown (preflight job)", td.get("pass") is True and not (td.get("report") or {}).get("leftover_containers") and not (td.get("report") or {}).get("leftover_networks") and td.get("live_tokens") == [],
             f"leftovers={(td.get('report') or {}).get('leftover_containers')}/{(td.get('report') or {}).get('leftover_networks')}; live tokens={td.get('live_tokens')}")
    if o.token_dir:
        live = []
        for p in Path(o.token_dir).glob("*.json"):
            try:
                if json.loads(p.read_text()).get("job_id", "").startswith("llm"): live.append(p.name[:12])
            except Exception: live.append(p.name[:12] + "?")
        rep.item("no live llm-job token now (token directory)", not live, f"live={live}")
    if o.stage == "preflight":
        rep.item("preflight stage: no episode, not paid", summ.get("paid") is False and runs == [], f"paid={summ.get('paid')}; episodes={len(runs)}")
    else:
        caps = summ.get("caps") or {}
        rep.item("rehearsal caps below any call's worst case", 0 < caps.get("episode_usd", 1) < 1e-5 and 0 < caps.get("total_usd", 1) < 1e-5, caps)
        ok_eps = [r for r in runs if r.get("category") == "budget_stop" and (r.get("spend") or {}).get("calls") == 0 and (r.get("spend") or {}).get("budget_refusals", 0) > 0
                  and (r.get("spend") or {}).get("cost_usd") == 0 and r.get("token_revoked") is True and (r.get("isolation_gate") or {}).get("pass") is True
                  and r.get("live_tokens_after") == [] and not (r.get("teardown") or {}).get("leftover_containers") and not (r.get("teardown") or {}).get("leftover_networks")]
        rep.item("rehearsal episodes: budget_stop, 0 calls, refusals > 0, gate passed, token revoked, $0, clean teardown", len(runs) == 2 and len(ok_eps) == 2,
                 [(r.get("run_id"), r.get("category"), (r.get("spend") or {}).get("calls"), (r.get("spend") or {}).get("budget_refusals"), r.get("token_revoked"), (r.get("isolation_gate") or {}).get("pass")) for r in runs])
    print("P3 CHECK PASS" if rep.ok else "P3 CHECK FAIL")
    return 0 if rep.ok else 1

if __name__ == "__main__":
    sys.exit(main())
