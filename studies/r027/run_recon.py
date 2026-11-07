"""Host side: the refill-v1 job topology (afd.jobs.Job + afd.adapters.refill.start, condition 'fabricated', seed 42),
a fresh pinned browser, and the reconstruction container on the CDP network ONLY (no gateway, no model, no token).
Reads backend state via the site's own API before/after each click. Writes recon.json next to this file."""
import json, sys, time
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent   # studies/r027/
from afd import config as C, dockerops as D, jobs as J, gateway as G, firewall as F  # noqa: E402
from afd.adapters import refill as R  # noqa: E402

def meds(job, sid): return {m["id"]: (m["medication"], m["refills_remaining"]) for m in J.http_json(f"{job.backend}/api/env/patient_portal/medications?session_id={sid}")["items"]}

F.install(); G.up()
with J.Job(budget_usd=0.0, label="m0recon") as job:
    ctx = R.start(job, "fabricated")
    sid = ctx["session_id"]; before = meds(job, sid)
    browser, recon = f"afd-{job.id}-browser", f"afd-{job.id}-recon"
    try:
        D.sh("docker", "run", "-d", "--name", browser, "--network", job.nets["site"], *D.labels(job.id, "browser"), *D.hardening("browser", C.BROWSER_UID), C.IMAGES["browser"])
        D.sh("docker", "network", "connect", job.nets["cdp"], browser)
        cdp_ip = D.ip_of(browser, job.nets["cdp"]); J.wait_cdp(cdp_ip)
        env = {"AFD_CDP_URL": f"http://{cdp_ip}:9222", "AFD_LAUNCH_URL": ctx["launch_url"], "AFD_ALLOWED_HOST": job.site_ip}
        r = D.sh("docker", "run", "--name", recon, "--network", job.nets["cdp"], *D.labels(job.id, "worker"), *D.hardening("worker", C.WORKER_UID),
                 *[x for k, v in env.items() for x in ("-e", f"{k}={v}")], "-v", f"{HERE / 'reconstruct.py'}:/afd/reconstruct.py:ro",
                 "--entrypoint", "python", C.IMAGES["worker"], "/afd/reconstruct.py", check=False, timeout=300)
        after = meds(job, sid); eff = job.control(f"/_drill/effects?session={sid}")
        out = json.loads(r.stdout.split("RECON:", 1)[1]) if "RECON:" in r.stdout else {"error": r.stderr[-1500:]}
        out.update({"session_seed": 42, "condition": "fabricated", "variant": R.VARIANT, "meds_before": before, "meds_after": after,
                    "site_requests": eff.get("requests", []), "site_effects": eff.get("effects", []), "images": {k: C.IMAGES[k] for k in ("site", "browser", "worker")}})
    finally:
        D.remove_container(recon); D.remove_container(browser)
(HERE / "recon.json").write_text(json.dumps(out, indent=1, default=str))
print("teardown", job.teardown_report)
