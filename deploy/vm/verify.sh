#!/bin/bash
# Mandatory security suite for the rootless deployment (root). Stops the orchestrator daemon while it runs, then restores it.
# Writes $AFD_ACCEPTANCE/latest.json and exits non-zero unless EVERY mandatory check passed. No inference cost.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
ROOT=/opt/afd; U=$(id -u afd-orch); RUN=/run/user/$U
as_orch() { sudo -u afd-orch -H env XDG_RUNTIME_DIR="$RUN" DBUS_SESSION_BUS_ADDRESS="unix:path=$RUN/bus" PATH=/usr/bin:/bin:/usr/sbin:/sbin "$@"; }
as_orch systemctl --user stop afd-orchd.service || true
trap 'as_orch systemctl --user start afd-orchd.service || true' EXIT
AFD_ENV=vm python3 -I "$ROOT/tests/acceptance/rootless_suite.py" "$@"
