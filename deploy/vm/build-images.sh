#!/bin/bash
# Build the four images in afd-orch's rootless Docker. Image builds need registry/PyPI/npm access, so this opens a
# TEMPORARY build window (afd-orch: TCP 80/443 + DNS) and always re-locks the firewall on exit. It refuses to run while any
# Agent Fire Drill container exists, and stops the orchestrator daemon for the duration.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
ROOT=/opt/afd; . /etc/afd/afd.env
U=$(id -u afd-orch); RUN=/run/user/$U
as_orch() { sudo -u afd-orch -H env XDG_RUNTIME_DIR="$RUN" DBUS_SESSION_BUS_ADDRESS="unix:path=$RUN/bus" DOCKER_HOST="unix://$RUN/docker.sock" PATH=/usr/bin:/bin:/usr/sbin:/sbin "$@"; }
n=$(as_orch docker ps -aq --filter label=afd.managed | wc -l)
[ "$n" = 0 ] || { echo "refusing: $n Agent Fire Drill container(s) exist; run the janitor first (afdctl janitor)" >&2; exit 1; }
as_orch systemctl --user stop afd-orchd.service || true
trap '"$ROOT/deploy/vm/afd-firewall.sh" apply; as_orch systemctl --user start afd-orchd.service || true' EXIT
"$ROOT/deploy/vm/afd-firewall.sh" build-window
SRC=/var/lib/afd-orch/build-src
as_orch mkdir -p "$SRC"
rsync -a --delete --exclude-from="$ROOT/deploy/vm/build-context.exclude" "$ROOT/" "$SRC/"
[ -s "$SRC/third_party/breakingweb.lock" ] || { echo "build context is missing third_party/breakingweb.lock" >&2; exit 1; }
chown -R afd-orch:afd-orch "$SRC"
as_orch bash -c "cd '$SRC' && make images"
as_orch docker image inspect --format '{{index .RepoTags 0}} {{.Id}}' afd/site:1 afd/browser:1 afd/worker:1 afd/gateway:1 \
  | tee "$AFD_VAR/images.txt"
chown afd-orch:afd-orch "$AFD_VAR/images.txt"
echo "images built; firewall re-locked on exit"
