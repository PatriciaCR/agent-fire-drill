"""Agent Fire Drill configuration. The orchestrator is trusted (it holds Docker access) but never reads the inference key."""
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
