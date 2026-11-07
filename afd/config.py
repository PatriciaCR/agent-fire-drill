"""Agent Fire Drill configuration. The orchestrator is trusted (it holds Docker access) but never reads the inference key.

Two deployment modes (AFD_MODE):
  rootful   development: one rootful Docker daemon; the gateway is a container on the egress network; host firewall via sudo.
  rootless  VM: the orchestrator is the unprivileged user afd-orch running rootless Docker, executing INSIDE the rootless
            network namespace (deploy/vm/afd-ns). The gateway is a host systemd service (user afd-gw) on a dedicated host
            address; the host firewall (deploy/vm/afd-firewall.sh, root) confines every packet afd-orch's daemon sends to
            that address. The orchestrator has no sudo and cannot read the key (OS-enforced: different uid, no Docker
            daemon that could mount host files as root)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR = Path(os.environ.get("AFD_VAR", ROOT / "var"))          # job evidence (gitignored)
JOBS_DIR = VAR / "jobs"
# Gateway state MUST live on a filesystem that enforces ownership (not a host-shared mount): verified in practice that a
# host-mounted project directory silently ignores chown, which would leave the key readable by the orchestrator.
STATE = Path(os.environ.get("AFD_STATE", "/var/lib/afd"))
TOKEN_DIR, LEDGER_DIR, SECRET_DIR = STATE / "tokens", STATE / "ledger", STATE / "secrets"   # key: SECRET_DIR/vultr_key, 0400, GATEWAY_UID
KEY_FILE = SECRET_DIR / "vultr_key"

MODE = os.environ.get("AFD_MODE", "rootful")
if MODE not in ("rootful", "rootless"): raise SystemExit(f"AFD_MODE must be rootful or rootless, not {MODE!r}")
ROOTLESS = MODE == "rootless"
GATEWAY_ADDR = os.environ.get("AFD_GATEWAY_ADDR", "10.233.0.1")   # rootless: host gateway service address (dummy interface afd0)
GATEWAY_PORT = int(os.environ.get("AFD_GATEWAY_PORT", "8080"))
RESULTS_DIR = Path(os.environ.get("AFD_RESULTS", VAR / "qualifications"))   # evidence bundles (web reads, read-only)
SPOOL_DIR = Path(os.environ.get("AFD_SPOOL", VAR / "spool"))                 # web -> orchestrator requests
ACCEPTANCE_DIR = Path(os.environ.get("AFD_ACCEPTANCE", VAR / "acceptance"))  # latest containment evidence

IMAGES = {"site": "afd/site:1", "browser": "afd/browser:1", "worker": "afd/worker:1", "gateway": "afd/gateway:1"}
GATEWAY_UID, WORKER_UID, BROWSER_UID, SITE_UID = 10001, 10002, 65534, 65534
GATEWAY_NAME, EGRESS_NET = "afd-gateway", "afd-egress"
EGRESS_SUBNET = "10.232.0.0/29"          # gateway's only route out; firewall allows TCP 443 to Vultr inference only
JOB_POOL = "10.231.0.0/16"               # every per-job network is a /28 inside this pool
INFERENCE_HOST = "api.vultrinference.com"
LABEL = "afd.managed"

MODEL = os.environ.get("AFD_MODEL", "glm-5.3-flash")
MAX_TOKENS = 8192
LIMITS = {  # per container: (memory, cpus, pids, tmpfs size)
    "site": ("1g", "2", 256, "64m"), "browser": ("2g", "2", 512, "512m"), "worker": ("1536m", "2", 256, "512m"), "gateway": ("256m", "1", 128, "16m")}
RUN_WALLCLOCK_S = int(os.environ.get("AFD_RUN_WALLCLOCK_S", "900"))
JOB_BUDGET_USD = float(os.environ.get("AFD_JOB_BUDGET_USD", "0.60"))
# Spend guards (the orchestrator daemon enforces the daily cap and smoke quota; the gateway enforces each job's budget)
DAILY_BUDGET_USD = float(os.environ.get("AFD_DAILY_BUDGET_USD", "2.00"))
SMOKE_BUDGET_USD = float(os.environ.get("AFD_SMOKE_BUDGET_USD", "0.05"))
SMOKES_PER_DAY = int(os.environ.get("AFD_SMOKES_PER_DAY", "20"))
