#!/bin/bash
# DEVELOPMENT SANDBOX ONLY: run the rootless security suite against the emulation. Can never produce passing VM evidence.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
AFD_ENV=dev AFD_ENV_FILE=/etc/afd-dev/afd.env AFD_KEY_SOURCE=/etc/afd-dev/vultr_key python3 -I "$REPO/tests/acceptance/rootless_suite.py"
