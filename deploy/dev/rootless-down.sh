#!/bin/bash
# DEVELOPMENT SANDBOX ONLY: remove the rootless emulation. Keeps the users and the rootful development setup.
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
U=$(id -u afd-orch)
pkill -u afd-gw -f gateway.py; pkill -u afd-orch; sleep 2
AFD_ENV_FILE=/etc/afd-dev/afd.env "$REPO/deploy/vm/afd-firewall.sh" remove
ip link del afd0 2>/dev/null
[ -f /etc/afd-dev/vultr_key ] && shred -u /etc/afd-dev/vultr_key
rm -rf /etc/afd-dev /var/lib/afd-dev /var/lib/afd-orch/.local/share/docker /var/lib/afd-orch/.config /run/user/$U
chown agent:agent /dev/fuse; chmod 0600 /dev/fuse; rm -f /dev/net/tun
echo "dev rootless emulation removed"
