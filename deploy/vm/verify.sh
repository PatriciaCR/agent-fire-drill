#!/bin/bash
# Mandatory security suite for the rootless deployment (root). Stops the orchestrator daemon while it runs, then restores it.
# Writes $AFD_ACCEPTANCE/latest.json and exits non-zero unless EVERY mandatory check passed. No inference cost.
# After the suite exits, this script independently confirms that the suite's temporary probe listener (port 18080) is
# closed and its INPUT guard chain is gone; if not, it marks the evidence as failed, so expose.sh will refuse it.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
ROOT=/opt/afd; U=$(id -u afd-orch); RUN=/run/user/$U; PROBE_PORT=18080
. /etc/afd/afd.env
as_orch() { sudo -u afd-orch -H env XDG_RUNTIME_DIR="$RUN" DBUS_SESSION_BUS_ADDRESS="unix:path=$RUN/bus" PATH=/usr/bin:/bin:/usr/sbin:/sbin "$@"; }
drop_guard() { for t in iptables ip6tables; do while $t -w -C INPUT -j AFD-PROBE 2>/dev/null; do $t -w -D INPUT -j AFD-PROBE; done
               $t -w -n -L AFD-PROBE >/dev/null 2>&1 && { $t -w -F AFD-PROBE; $t -w -X AFD-PROBE; }; done; true; }
as_orch systemctl --user stop afd-orchd.service || true
trap 'drop_guard; as_orch systemctl --user start afd-orchd.service || true' EXIT
rc=0; AFD_ENV=vm python3 -I "$ROOT/tests/acceptance/rootless_suite.py" "$@" || rc=$?
left=$(ss -ltnH "sport = :$PROBE_PORT" | wc -l)
guard=$( (iptables -w -n -L AFD-PROBE; ip6tables -w -n -L AFD-PROBE) 2>/dev/null | grep -c '^Chain' || true)
if [ "$left" = 0 ] && [ "$guard" = 0 ]; then
  echo "post-run: probe listener on :$PROBE_PORT closed; guard chain removed"
else
  echo "post-run: FAIL: $left listener(s) on :$PROBE_PORT, $guard guard chain(s) left; marking evidence as failed" >&2
  python3 -I - "$AFD_ACCEPTANCE/latest.json" "$left" "$guard" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d["checks"]["post_run_probe_listener_closed"] = {"status": "FAIL", "mandatory": True, "detail": {"listeners": int(sys.argv[2]), "guard_chains": int(sys.argv[3])}}
d["pass"] = False; json.dump(d, open(p, "w"), indent=1)
PY
  rc=1
fi
exit $rc
