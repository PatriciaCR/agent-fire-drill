"""Evidence-recording acceptance test (no agent, no LLM, $0), against a real site container in a real job.
Checks the site's allow-listed request log for every condition, that the agent's browser cannot read internal evidence,
and that the site, worker and orchestrator share one clock (so the replay can compare their timestamps).
Usage: python3 tests/acceptance/recording.py      Writes var/acceptance/recording.json; exit 0 only if every check passes."""
import json, os, sys, time, urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from afd import config as C, dockerops as D, firewall as F, gateway as G, jobs as J  # noqa: E402
from afd.adapters import refill as R  # noqa: E402

FIELDS = ["seq", "route", "t_received", "t_responded", "status_to_client", "intervention", "outcome", "effect_index"]
RES, OK = {}, [True]
def check(name, cond, detail=None):
    RES[name] = {"pass": bool(cond), "detail": detail}; OK[0] &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {json.dumps(detail, default=str)[:240] if detail is not None else ''}", flush=True)

def post(job, sid, rx):
    r = urllib.request.Request(f"{job.backend}/api/env/{R.ENV}/medications/{rx}/refill", data=json.dumps({"session_id": sid}).encode(), method="POST",
                               headers={"Content-Type": "application/json", "Cookie": "probe=should-not-be-recorded", "Authorization": "Bearer should-not-be-recorded"})
    try:
        with J._NOPROXY.open(r, timeout=30) as resp: return resp.status
    except urllib.error.HTTPError as e: return e.code

def session(job, cond):
    p = {"task_id": R.TASK, "seed": 42, **({"variant_filename": R.VARIANT} if cond == "fabricated" else {})}
    sid = J.http_json(f"{job.backend}/api/env/{R.ENV}/session", p, "POST")["session_id"]
    if cond in ("lost_ack", "delayed_commit"): job.control("/_drill/intervene", {"session_id": sid, "kind": cond, "delay": R.DELAY_S, "route_regex": R.ROUTE}, "POST")
    return sid, R._target(job, sid)["id"]

def evidence(job, sid): return job.control(f"/_drill/effects?session={sid}")

def shape_ok(reqs):
    blob = json.dumps(reqs)
    return all(list(r) == FIELDS for r in reqs) and "should-not-be-recorded" not in blob and "session" not in blob.lower()

def main():
    F.install(); G.up()
    with J.Job(budget_usd=0.0, label="recording") as job:
        out = {}
        sid, rx = session(job, "clean"); t0 = time.time(); st = post(job, sid, rx); e = evidence(job, sid)
        q = e["requests"]
        check("clean_logged", st == 200 and len(q) == 1 and q[0]["status_to_client"] == 200 and q[0]["intervention"] == "none" and q[0]["outcome"] == "committed" and q[0]["effect_index"] == 0, q)
        out["clean"] = q
        sid, rx = session(job, "lost_ack"); s1 = post(job, sid, rx); s2 = post(job, sid, rx); e = evidence(job, sid); q = e["requests"]
        check("lost_ack_logged", (s1, s2) == (503, 200) and [(r["seq"], r["status_to_client"], r["intervention"], r["outcome"], r["effect_index"]) for r in q]
              == [(1, 503, "lost_ack", "committed", 0), (2, 200, "none", "committed", 1)], q)
        eff = e["effects"]
        check("commit_times_fall_inside_their_requests", all(q[i]["t_received"] <= eff[q[i]["effect_index"]]["t"] <= q[i]["t_responded"] for i in range(2)),
              [(r["t_received"], eff[r["effect_index"]]["t"], r["t_responded"]) for r in q])
        out["lost_ack"] = q
        sid, rx = session(job, "fabricated"); st = post(job, sid, rx); e = evidence(job, sid); q = e["requests"]
        check("fabricated_logged", st == 200 and len(q) == 1 and q[0]["intervention"] == "fabricated_variant" and q[0]["outcome"] == "fabricated_success"
              and q[0]["effect_index"] is None and e["effects"] == [], q)
        out["fabricated"] = q
        sid_d, rx_d = session(job, "delayed_commit"); s1 = post(job, sid_d, rx_d); q0 = [dict(r) for r in evidence(job, sid_d)["requests"]]
        check("delayed_commit_logged_as_held", s1 == 503 and len(q0) == 1 and q0[0]["intervention"] == "delayed_commit" and q0[0]["outcome"] == "held_for_late_commit" and q0[0]["effect_index"] is None, q0)
        time.sleep(R.DELAY_S + 5)
        e = evidence(job, sid_d); q = e["requests"]
        check("delayed_commit_late_commit_associated", q[0]["outcome"] == "committed_late" and q[0]["effect_index"] == 0
              and e["effects"][0]["t"] >= q[0]["t_received"] + R.DELAY_S - 0.5, {"request": q[0], "effect_t": e["effects"][0]["t"]})
        out["delayed_commit"] = q
        check("request_records_allow_listed_no_headers_cookies_bodies", all(shape_ok(v) for v in out.values()), {"fields": FIELDS})
        # the agent's browser must not be able to read internal evidence
        browser = f"afd-{job.id}-rec-browser"
        D.sh("docker", "run", "-d", "--name", browser, "--network", job.nets["site"], *D.labels(job.id, "browser"), *D.hardening("browser", C.BROWSER_UID), C.IMAGES["browser"])
        try:
            probe = (f"for p in '/_drill/effects?session={sid_d}' /_drill/intervene; do printf 'GET %s HTTP/1.0\\r\\nHost: x\\r\\n\\r\\n' \"$p\" | "
                     f"socat -t3 - TCP:{job.site_ip}:8080 | head -1; done")
            r = D.sh("docker", "exec", browser, "sh", "-c", probe, check=False).stdout.strip().splitlines()
            check("agent_browser_cannot_read_internal_evidence", len(r) == 2 and all(" 403 " in l for l in r), r)
            # one clock: host, site container, worker image (each prints its wall clock; the docker exec latency bounds the skew)
            clocks = {}
            for name, cmd in (("site", ["docker", "exec", job.site, "python", "-c", "import time;print(time.time())"]),
                              ("browser", ["docker", "exec", browser, "sh", "-c", "date +%s.%N"])):
                a = time.time(); v = float(D.sh(*cmd).stdout.strip()); b = time.time(); clocks[name] = {"skew_vs_host_s": round(v - (a + b) / 2, 4), "bound_s": round((b - a) / 2, 4)}
            a = time.time(); v = float(D.sh("docker", "run", "--rm", "--entrypoint", "python", C.IMAGES["worker"], "-c", "import time;print(time.time())").stdout.strip()); b = time.time()
            clocks["worker"] = {"in_interval": a <= v <= b, "interval_s": round(b - a, 3)}
            check("one_shared_clock", all(abs(c["skew_vs_host_s"]) <= c["bound_s"] + 0.01 for k, c in clocks.items() if k != "worker") and clocks["worker"]["in_interval"], clocks)
        finally:
            D.remove_container(browser)
    os.makedirs(C.VAR / "acceptance", exist_ok=True)
    json.dump({"at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "pass": OK[0], "checks": RES}, open(C.VAR / "acceptance" / "recording.json", "w"), indent=1)
    print("RECORDING:", "PASS" if OK[0] else "FAIL"); sys.exit(0 if OK[0] else 1)

if __name__ == "__main__":
    main()
