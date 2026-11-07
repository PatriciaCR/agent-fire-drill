#!/bin/bash
# Agent Fire Drill VM installer (Ubuntu 24.04, run as root from the repository checked out at /opt/afd). Idempotent.
# It does NOT install the inference key (install-key.sh), build images (build-images.sh), run the security suite
# (verify.sh) or expose anything publicly (expose.sh, which refuses until verify.sh has passed).
#
# Result:
#   afd-gw    system user; runs afd-gateway.service; receives the key via systemd LoadCredential (cannot read the file)
#   afd-orch  system user; NO sudo, not in docker; rootless Docker (user service) + afd-orchd (inside its namespaces)
#   afd-web   system user; afd-web.service on 127.0.0.1 only; no Docker, no key
#   rootful Docker: installed only as a dependency of the rootless extras, then disabled and masked
#   host firewall (afd-firewall.service): per-user egress rules, applied before the rootless daemon can start
set -euo pipefail
ROOT=/opt/afd
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
[ "$(cd "$(dirname "$0")/../.." && pwd)" = "$ROOT" ] || { echo "the repository must be checked out at $ROOT" >&2; exit 1; }
. /etc/os-release; [ "$ID" = ubuntu ] && [ "${VERSION_ID%%.*}" -ge 24 ] || { echo "expected Ubuntu 24.04+, found $PRETTY_NAME" >&2; exit 1; }
log() { printf '\n== %s\n' "$*"; }

log "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg uidmap dbus-user-session iptables python3 git make rsync slirp4netns
DOCKER_FPR=9DC858229FC7DD38854AE2D88D81803C0EBFCD88   # Docker's published Release signing key fingerprint
if [ ! -s /etc/apt/keyrings/docker.asc ]; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.asc
  got=$(gpg --show-keys --with-colons /tmp/docker.asc | awk -F: '/^fpr/ {print $10; exit}')
  [ "$got" = "$DOCKER_FPR" ] || { echo "Docker apt key fingerprint mismatch: $got" >&2; exit 1; }
  install -m 0644 /tmp/docker.asc /etc/apt/keyrings/docker.asc; rm -f /tmp/docker.asc
fi
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-ce-rootless-extras

log "disable and mask rootful Docker (nobody gets a root-equivalent Docker socket)"
systemctl disable --now docker.socket docker.service containerd.service 2>/dev/null || true
systemctl mask docker.socket docker.service containerd.service
rm -f /var/run/docker.sock

log "users"
for u in afd-orch afd-gw afd-web; do
  id "$u" >/dev/null 2>&1 && continue
  if [ "$u" = afd-orch ]; then useradd --system --create-home --home-dir /var/lib/afd-orch --shell /usr/sbin/nologin "$u"
  else useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$u"; fi
done
for g in sudo docker adm lxd admin wheel; do getent group "$g" >/dev/null && gpasswd -d afd-orch "$g" >/dev/null 2>&1 || true; done
for f in /etc/subuid /etc/subgid; do
  grep -q '^afd-orch:' "$f" && continue
  start=$(awk -F: '{e=$2+$3; if (e>m) m=e} END {print (m>300000 ? m : 300000)}' "$f")
  echo "afd-orch:$start:65536" >> "$f"
done
ORCH_UID=$(id -u afd-orch)

log "AppArmor profile for RootlessKit (Ubuntu restricts unprivileged user namespaces)"
install -m 0644 "$ROOT/deploy/vm/apparmor-rootlesskit" /etc/apparmor.d/usr.bin.rootlesskit
apparmor_parser -r /etc/apparmor.d/usr.bin.rootlesskit

log "cgroup delegation and boot ordering"
install -d /etc/systemd/system/user@.service.d "/etc/systemd/system/user@$ORCH_UID.service.d"
install -m 0644 "$ROOT/deploy/vm/systemd/user-delegate.conf" /etc/systemd/system/user@.service.d/delegate.conf
install -m 0644 "$ROOT/deploy/vm/systemd/user-afd-orch.conf" "/etc/systemd/system/user@$ORCH_UID.service.d/afd.conf"

log "configuration and state directories"
install -d -m 0755 -o root -g root /etc/afd
[ -f /etc/afd/afd.env ] || install -m 0644 "$ROOT/deploy/vm/afd.env.default" /etc/afd/afd.env
. /etc/afd/afd.env
install -d -m 0755 -o root -g root "$AFD_STATE"
install -d -m 2750 -o afd-orch -g afd-gw   "$AFD_STATE/tokens"      # orchestrator writes 0640 token files; gateway reads
install -d -m 2750 -o afd-gw   -g afd-orch "$AFD_STATE/ledger"      # gateway writes 0640 usage rows; orchestrator reads
install -d -m 0750 -o afd-orch -g afd-orch "$AFD_VAR"               # job working files (orchestrator only)
install -d -m 2750 -o afd-orch -g afd-web  "$AFD_RESULTS"           # evidence bundles: web reads
install -d -m 2770 -o afd-web  -g afd-orch "$AFD_SPOOL"             # web -> orchestrator requests
install -d -m 2755 -o root     -g afd-web  "$AFD_ACCEPTANCE"        # containment evidence: written by root (verify.sh)

log "system units"
for u in afd-net.service afd-firewall.service afd-firewall-refresh.service afd-firewall-refresh.timer afd-gateway.service afd-web.service; do
  install -m 0644 "$ROOT/deploy/vm/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now afd-net.service afd-firewall.service afd-firewall-refresh.timer
systemctl enable afd-gateway.service afd-web.service
if [ -s /etc/afd/vultr_key ]; then systemctl restart afd-gateway.service; else echo "(gateway not started: no key yet; run install-key.sh)"; fi
systemctl restart afd-web.service

log "rootless Docker for afd-orch (starts only after the firewall: see user@$ORCH_UID.service.d/afd.conf)"
loginctl enable-linger afd-orch
systemctl start "user@$ORCH_UID.service"
RUN="/run/user/$ORCH_UID"
for _ in $(seq 30); do [ -S "$RUN/bus" ] && break; sleep 1; done
as_orch() { sudo -u afd-orch -H env XDG_RUNTIME_DIR="$RUN" DBUS_SESSION_BUS_ADDRESS="unix:path=$RUN/bus" PATH=/usr/bin:/bin:/usr/sbin:/sbin "$@"; }
if ! as_orch systemctl --user is-active --quiet docker.service; then
  as_orch dockerd-rootless-setuptool.sh install
fi
as_orch mkdir -p /var/lib/afd-orch/.config/systemd/user
install -m 0644 -o afd-orch -g afd-orch "$ROOT/deploy/vm/systemd/afd-orchd.user.service" /var/lib/afd-orch/.config/systemd/user/afd-orchd.service
as_orch systemctl --user daemon-reload
as_orch systemctl --user enable afd-orchd.service
as_orch env DOCKER_HOST="unix://$RUN/docker.sock" docker info --format 'rootless daemon: {{.ServerVersion}} cgroup={{.CgroupDriver}}/{{.CgroupVersion}} security={{.SecurityOptions}}'

log "done"
cat <<EOF
Next steps (in order):
  sudo $ROOT/deploy/vm/install-key.sh       # paste the Vultr inference key (hidden input)
  sudo $ROOT/deploy/vm/build-images.sh      # temporary build egress window for afd-orch, then re-locked
  sudo $ROOT/deploy/vm/verify.sh            # mandatory security suite; writes $AFD_ACCEPTANCE/latest.json
  sudo $ROOT/deploy/vm/expose.sh            # refuses unless the latest verification passed on this commit
EOF
