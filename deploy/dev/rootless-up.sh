#!/bin/bash
# DEVELOPMENT SANDBOX ONLY: emulate the VM's rootless layout without systemd, to exercise the rootless code paths and the
# security suite here. Differences from the VM (all reported by the suite as SKIPPED or FAIL, never PASS):
#   - no systemd: the gateway is started by root with setpriv (uid afd-gw, no-new-privs) and receives the key on an
#     inherited pipe instead of LoadCredential; rootless dockerd is started directly, not as a user service
#   - no systemd => rootless Docker has NO cgroups: per-container CPU/memory/PID limits are NOT enforced here
#   - sandbox workarounds: /dev/net/tun created, /dev/fuse opened to all users, fuse-overlayfs storage
#   - the rootful development daemon keeps running (afd-orch has no access to it)
# Separate state (/etc/afd-dev, /var/lib/afd-dev) so the rootful development setup is untouched. Undo: rootless-down.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
E=/etc/afd-dev; S=/var/lib/afd-dev
install -d -m 0755 $E
cat > $E/afd.env <<ENV
AFD_MODE=rootless
AFD_STATE=$S
AFD_VAR=$S/var
AFD_RESULTS=$S/results
AFD_SPOOL=$S/spool
AFD_ACCEPTANCE=$S/acceptance
AFD_GATEWAY_ADDR=10.233.0.1
AFD_GATEWAY_PORT=8080
AFD_WEB_BIND=127.0.0.1
AFD_WEB_PORT=8090
AFD_INFERENCE_HOST=api.vultrinference.com
AFD_DAILY_BUDGET_USD=0.50
AFD_SMOKE_BUDGET_USD=0.05
AFD_SMOKES_PER_DAY=5
ENV
for u in afd-orch afd-gw afd-web; do id $u >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin $u; done
grep -q '^afd-orch:' /etc/subuid || echo 'afd-orch:200000:65536' >> /etc/subuid
grep -q '^afd-orch:' /etc/subgid || echo 'afd-orch:200000:65536' >> /etc/subgid
install -d -m 0755 -o root -g root $S
install -d -m 2750 -o afd-orch -g afd-gw   $S/tokens
install -d -m 2750 -o afd-gw   -g afd-orch $S/ledger
install -d -m 0750 -o afd-orch -g afd-orch $S/var
install -d -m 2750 -o afd-orch -g afd-web  $S/results
install -d -m 2770 -o afd-web  -g afd-orch $S/spool
install -d -m 2755 -o root     -g afd-web  $S/acceptance
# key: root:root 0400 copy of the development key file (never printed)
if [ ! -s $E/vultr_key ]; then (umask 077; cat /var/lib/afd/secrets/vultr_key > $E/vultr_key); chown root:root $E/vultr_key; chmod 0400 $E/vultr_key; fi
# gateway address
ip link show afd0 >/dev/null 2>&1 || ip link add afd0 type dummy
ip addr replace 10.233.0.1/32 dev afd0; ip link set afd0 up
# host firewall (the same script the VM uses)
AFD_ENV_FILE=$E/afd.env "$REPO/deploy/vm/afd-firewall.sh" apply
# gateway: uid afd-gw, no-new-privs, key on an inherited pipe (the source file stays unreadable to afd-gw)
pkill -u afd-gw -f gateway.py 2>/dev/null || true; sleep 1
cat $E/vultr_key | (umask 0027; exec setpriv --reuid afd-gw --regid afd-gw --init-groups --no-new-privs --inh-caps=-all --bounding-set=-all \
  env -i AFD_KEY_FILE=- AFD_BIND=10.233.0.1 AFD_PORT=8080 AFD_TOKEN_DIR=$S/tokens AFD_LEDGER_DIR=$S/ledger PATH=/usr/bin:/bin \
  python3 -I "$REPO/images/gateway/gateway.py") > /dev/null 2>&1 &
# rootless dockerd for afd-orch (sandbox workarounds)
install -d -m 0750 -o afd-orch -g afd-orch /var/lib/afd-orch
mkdir -p /dev/net; [ -e /dev/net/tun ] || mknod -m 0666 /dev/net/tun c 10 200
chmod 0666 /dev/fuse
U=$(id -u afd-orch); install -d -m 0700 -o afd-orch -g afd-orch /run/user/$U
sudo -u afd-orch mkdir -p /var/lib/afd-orch/.config/docker
echo '{"storage-driver":"fuse-overlayfs","features":{"containerd-snapshotter":false}}' > /var/lib/afd-orch/.config/docker/daemon.json
chown afd-orch:afd-orch /var/lib/afd-orch/.config/docker/daemon.json
if [ ! -S /run/user/$U/docker.sock ] || ! sudo -u afd-orch env DOCKER_HOST=unix:///run/user/$U/docker.sock docker info >/dev/null 2>&1; then
  sudo -u afd-orch -H env -i HOME=/var/lib/afd-orch XDG_RUNTIME_DIR=/run/user/$U PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \
    bash -c 'nohup dockerd-rootless.sh > /var/lib/afd-orch/dockerd.log 2>&1 &'
  for _ in $(seq 40); do sudo -u afd-orch env DOCKER_HOST=unix:///run/user/$U/docker.sock docker info >/dev/null 2>&1 && break; sleep 1; done
fi
# images: copy from the rootful development daemon (no registry egress needed)
for img in afd/site:1 afd/browser:1 afd/worker:1 afd/gateway:1; do
  want=$(docker image inspect --format '{{.Id}}' $img)
  have=$(sudo -u afd-orch env DOCKER_HOST=unix:///run/user/$U/docker.sock docker image inspect --format '{{.Id}}' $img 2>/dev/null || true)
  [ "$want" = "$have" ] || docker save $img | sudo -u afd-orch env DOCKER_HOST=unix:///run/user/$U/docker.sock docker load -q
done
sleep 1; curl -s --noproxy '*' --max-time 3 http://10.233.0.1:8080/healthz && echo "  <- gateway (host service, uid afd-gw)"
echo "dev rootless emulation up. Verify: sudo $REPO/deploy/dev/rootless-verify.sh"
