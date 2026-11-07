"""Run the booking-form reproduction in a diagnostic job (no inference; no gateway use). Counts appointments per session
from the backend. Diagnostic only; separate from the frozen booking check."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from afd import config as C, dockerops as D, jobs as J  # noqa: E402
from afd.adapters import booking as BK  # noqa: E402
with J.Job(budget_usd=0.0, label="diagbooking") as job:
    a, b = BK.start(job, "clean"), BK.start(job, "clean")
    br = f"afd-{job.id}-diag-browser"
    D.sh("docker", "run", "-d", "--name", br, "--network", job.nets["site"], *D.labels(job.id, "browser"), *D.hardening("browser", C.BROWSER_UID), C.IMAGES["browser"])
    D.sh("docker", "network", "connect", job.nets["cdp"], br); ip = D.ip_of(br, job.nets["cdp"]); J.wait_cdp(ip)
    w = f"afd-{job.id}-diag-pw"
    D.sh("docker", "create", "--name", w, "--network", job.nets["cdp"], *D.labels(job.id, "worker"), *D.hardening("worker", C.WORKER_UID),
         "-e", f"CDP=http://{ip}:9222", "-e", "URL_A=" + a["launch_url"].replace("/patient_portal/?", "/patient_portal/appointments?"), "-e", "URL_B=" + b["launch_url"].replace("/patient_portal/?", "/patient_portal/appointments?"), "afd/diag-booking:0")
    r = D.sh("docker", "start", "-a", w, check=False, timeout=300); D.remove_container(w); D.remove_container(br)
    R = json.loads(r.stdout.split("REPRO:", 1)[1]) if "REPRO:" in r.stdout else {"error": r.stdout[-800:] + r.stderr[-800:]}
    R["backend_new_appointments_session_A_browseruse_js"] = BK.finish(job, a, time.time())["backend_effects"]
    R["backend_new_appointments_session_B_playwright"] = BK.finish(job, b, time.time())["backend_effects"]
print(json.dumps(R, indent=1)); json.dump(R, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "repro_result.json"), "w"), indent=1)
