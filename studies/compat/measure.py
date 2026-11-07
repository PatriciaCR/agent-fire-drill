"""Compatibility-study measurements beyond the core pipeline (D3, D4, D8, AM-5). Generic over the contract plus a
per-app study config (studies/compat/apps/<app>.json) naming a SECOND, independent count source for the oracle
cross-check. No application-specific code. Usage: python3 studies/compat/measure.py <app>   ($0; local sandbox only)"""
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from afd import contract as K, dockerops as D, firewall as F, gateway as G, generic as GEN  # noqa: E402

def second_source(job, sc, spec):
    """Count from an independent second source (e.g. the app's API with the agent's identity, or another datastore view)."""
    r = K.render(spec, **sc.vars)
    if "exec" in r:                                  # the app's own CLI (e.g. its order API), run inside its container
        out = D.sh("docker", "exec", job.containers[r["service"]], *r["exec"], check=False).stdout
        import re as _re; return int(_re.findall(r"-?\d+", out)[-1])
    st, doc, _, _ = K.http(job.base(r["service"]), r["method"], r["path"], headers=r.get("headers"))
    if st >= 300: raise RuntimeError(f"second source HTTP {st}")
    v = K.pointer(doc, r.get("pointer", ""))
    if r.get("filter_to") and isinstance(v, list):   # count list entries addressed to the scope (message lists)
        v = [m for m in v if any(str(t.get("Address", "")).lower() == str(r["filter_to"]).lower() for t in (m.get("To") or []))]
    return len(v) if isinstance(v, list) else int(v)

def main(app):
    cfg = json.loads((ROOT / "studies" / "compat" / "apps" / f"{app}.json").read_text())
    c = K.load(ROOT / cfg["contract"]); out = {"app": app, "contract": cfg["contract"], "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    F.install(); G.up()
    with GEN.GenericJob(c, "measure") as job:
        # D3 + D4: five scenarios with planned effect counts, then every oracle reading vs the second source, all at the end
        plan = [0, 1, 2, 1, 0]; scen = []; accepted = []; rejected = []
        succ = c["action"].get("success_status") or list(range(200, 300))
        for n in plan:
            s = GEN.Scenario(job); ok = bad = 0
            for _ in range(n):
                st = GEN.act(job, s)
                if st in succ: ok += 1
                else: bad += 1                       # e.g. the app refusing a repeat (native duplicate prevention)
            scen.append(s); accepted.append(ok); rejected.append(bad)
        rows = [{"attempts": n, "accepted": a, "rejected": r, "oracle": s.effects(), "second_source": second_source(job, s, cfg["second_source"])}
                for n, a, r, s in zip(plan, accepted, rejected, scen)]
        out["D3_oracle_cross_check"] = {"rows": rows, "pass": all(r["accepted"] == r["oracle"] == r["second_source"] for r in rows),
                                        "independence": c["oracle"].get("independence"), "second_source": cfg["second_source"].get("describe")}
        out["D4_isolation_no_contamination"] = {"pass": [r["oracle"] for r in rows] == accepted, "reset_method": cfg.get("reset_method"),
                                                "expected_from_accepted_attempts": accepted, "observed": [r["oracle"] for r in rows]}
        # AM-2: duplicate probe - with lost_ack armed, submit twice; record each status, the app's error code (allow-listed
        # field only), proxy evidence and the oracle. Classifies why a duplicate did or did not occur.
        s = GEN.Scenario(job); job.arm(s.scope, "lost_ack", 6); a = s.action_request(); probe = []
        for _ in range(2):
            st, doc, _, _ = K.http(job.proxy_base, a["method"], a["path"], json_body=a["body"], form=a["form"], headers=a["headers"])
            code = (doc or {}).get("code") if isinstance(doc, dict) else None
            probe.append({"status": st, "app_error_code": code})
        ev = job.evidence(s.scope); n = s.effects()
        applied = any(r["intervention"] == "lost_ack" for r in ev["requests"])
        if n >= 2: cls = "duplicate produced"
        elif not applied or len(ev["requests"]) < 2: cls = "fault-injection failure"
        elif n == 1 and probe[1]["status"] >= 400: cls = "native duplicate prevention"
        else: cls = "unclassified (inspect)"
        out["AM2_duplicate_probe"] = {"attempts": probe, "proxy_requests": [(r["status_to_client"], r["intervention"], r["outcome"]) for r in ev["requests"]],
                                      "verified": n, "classification": cls}
        # AM-5: read lag after a committed action (user check and oracle)
        s = GEN.Scenario(job); t0 = time.time(); GEN.act(job, s); t_commit = time.time()
        chk = s.check_request(); lag = {}
        for name, probe in (("oracle", lambda: s._raw() - s.baseline >= 1),
                            ("user_check", lambda: K.http(job.base(job.public["name"]), chk["method"], chk["path"], headers=chk["headers"])[0] == 200 and
                             K.pointer(K.select(K.http(job.base(job.public["name"]), chk["method"], chk["path"], headers=chk["headers"])[1], chk.get("select")), chk["value"]) in (chk.get("success_in") or []))):
            t = time.time()
            while time.time() - t < 60:
                try:
                    if probe(): break
                except Exception: pass
                time.sleep(0.1)
            lag[name] = round(time.time() - t_commit, 2)
        out["AM5_read_lag_s"] = {**lag, "note": "upper bound: includes polling interval (0.1 s) and request time; measured once"}
        # D8: containment probes from the agent network
        targets = [f"{ip}:{next(x['port'] for x in c['services'] if x['name'] == svc)}" for svc, ip in job.ips.items()] + ["1.1.1.1:443", "169.254.169.254:80"]
        probe = ("import json, socket, urllib.request, urllib.error\nR = {}\nfor t in " + json.dumps(targets) + ":\n  h, p = t.rsplit(':', 1)\n"
                 "  try: socket.create_connection((h, int(p)), 3).close(); R[t] = 'OPEN'\n  except Exception as e: R[t] = 'blocked:' + type(e).__name__\n"
                 "try: urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://afd-proxy:8080/_afd/evidence?scope=x', timeout=5); R['control'] = 200\n"
                 "except urllib.error.HTTPError as e: R['control'] = e.code\nexcept Exception as e: R['control'] = 'blocked:' + type(e).__name__\n"
                 "try: R['proxy_reachable'] = urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://afd-proxy:8080/', timeout=5).status\n"
                 "except urllib.error.HTTPError as e: R['proxy_reachable'] = e.code\nexcept Exception as e: R['proxy_reachable'] = 'blocked:' + type(e).__name__\nprint('PROBE:' + json.dumps(R))")
        r = D.sh("docker", "run", "--rm", "--network", job.nets["agent"], *D.labels(job.id, "probe"), *D.hardening("worker", 10002), "--entrypoint", "python", GEN.RUNTIME_IMAGE, "-c", probe, check=False)
        P = json.loads(r.stdout.split("PROBE:", 1)[1]) if "PROBE:" in r.stdout else {}
        out["D8_containment"] = {"probes": P, "pass": bool(P) and all(str(P[t]).startswith("blocked") for t in targets) and P.get("control") == 403
                                 and isinstance(P.get("proxy_reachable"), int)}
    out["teardown"] = job.teardown_report
    dst = ROOT / "studies" / "compat" / "results" / f"{app}-measure.json"; dst.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str)[:3000])

if __name__ == "__main__":
    main(sys.argv[1])
