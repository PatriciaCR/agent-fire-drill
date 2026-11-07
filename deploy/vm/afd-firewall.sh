#!/bin/bash
# Agent Fire Drill host firewall (root). The HARD boundary for the rootless deployment: per-uid egress rules in OUTPUT match
# every socket the service users open on the host, including slirp4netns, which carries all traffic leaving afd-orch's
# rootless Docker namespace.
#   afd-orch: only the gateway address:port (build-window mode adds TCP 80/443 and DNS for image builds, nothing else)
#   afd-gw:   DNS to the configured resolvers, TCP 443 to the resolved inference addresses, replies on accepted connections
#   afd-web:  replies on accepted connections only (it serves 127.0.0.1 behind the reverse proxy; it opens nothing)
#   INPUT:    the gateway address is reachable only via loopback (never from a real interface)
# IPv6: the same per-uid policy (inference AAAA allowed for afd-gw; nothing else).
# Usage: afd-firewall.sh apply | build-window | remove | show
set -euo pipefail
ENV_FILE=${AFD_ENV_FILE:-/etc/afd/afd.env}
[ -r "$ENV_FILE" ] && . "$ENV_FILE"
GW=${AFD_GATEWAY_ADDR:-10.233.0.1}; GWPORT=${AFD_GATEWAY_PORT:-8080}; HOST=${AFD_INFERENCE_HOST:-api.vultrinference.com}
ORCH=$(id -u afd-orch); GWU=$(id -u afd-gw); WEB=$(id -u afd-web)
[ "$(id -u)" = 0 ] || { echo "must run as root" >&2; exit 1; }
RESOLVERS4=$(awk '/^nameserver/ && $2 ~ /^[0-9.]+$/ {print $2}' /etc/resolv.conf | sort -u)
RESOLVERS6=$(awk '/^nameserver/ && $2 ~ /:/ {print $2}' /etc/resolv.conf | sort -u)

chain() { local t=$1 c=$2; $t -w -n -L "$c" >/dev/null 2>&1 || $t -w -N "$c"; $t -w -F "$c"; }
jump_first() { local t=$1 parent=$2 c=$3; while $t -w -C "$parent" -j "$c" 2>/dev/null; do $t -w -D "$parent" -j "$c"; done; $t -w -I "$parent" 1 -j "$c"; }
unjump() { local t=$1 parent=$2 c=$3; while $t -w -C "$parent" -j "$c" 2>/dev/null; do $t -w -D "$parent" -j "$c"; done
           if $t -w -n -L "$c" >/dev/null 2>&1; then $t -w -F "$c"; $t -w -X "$c"; fi; }

apply() {   # $1 = locked | build
  local mode=$1 v4 v6
  v4=$(getent ahostsv4 "$HOST" | awk '{print $1}' | sort -u); v6=$(getent ahostsv6 "$HOST" 2>/dev/null | awk '$1 ~ /:/ && $1 !~ /^::ffff:/ {print $1}' | sort -u || true)
  [ -n "$v4" ] || { echo "cannot resolve $HOST; refusing to install a firewall without the inference address (fail closed: previous rules kept)" >&2; exit 2; }
  for t in iptables ip6tables; do
    for c in AFD-OUT AFD-ORCH AFD-GW AFD-WEB; do chain $t $c; done
    $t -w -A AFD-OUT -m owner --uid-owner "$ORCH" -j AFD-ORCH
    $t -w -A AFD-OUT -m owner --uid-owner "$GWU" -j AFD-GW
    $t -w -A AFD-OUT -m owner --uid-owner "$WEB" -j AFD-WEB
  done
  # afd-orch (incl. slirp4netns): gateway only
  iptables -w -A AFD-ORCH -d "$GW" -p tcp --dport "$GWPORT" -j ACCEPT
  if [ "$mode" = build ]; then
    for r in $RESOLVERS4; do iptables -w -A AFD-ORCH -d "$r" -p udp --dport 53 -j ACCEPT; iptables -w -A AFD-ORCH -d "$r" -p tcp --dport 53 -j ACCEPT; done
    iptables -w -A AFD-ORCH -d 169.254.0.0/16 -j REJECT
    iptables -w -A AFD-ORCH -p tcp -m multiport --dports 80,443 -j ACCEPT
    for r in $RESOLVERS6; do ip6tables -w -A AFD-ORCH -d "$r" -p udp --dport 53 -j ACCEPT; done
    ip6tables -w -A AFD-ORCH -p tcp -m multiport --dports 80,443 -j ACCEPT
  fi
  iptables -w -A AFD-ORCH -j REJECT; ip6tables -w -A AFD-ORCH -j REJECT
  # afd-gw: replies to workers, DNS, inference 443
  for t in iptables ip6tables; do $t -w -A AFD-GW -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT; done
  for r in $RESOLVERS4; do iptables -w -A AFD-GW -d "$r" -p udp --dport 53 -j ACCEPT; iptables -w -A AFD-GW -d "$r" -p tcp --dport 53 -j ACCEPT; done
  for r in $RESOLVERS6; do ip6tables -w -A AFD-GW -d "$r" -p udp --dport 53 -j ACCEPT; ip6tables -w -A AFD-GW -d "$r" -p tcp --dport 53 -j ACCEPT; done
  for ip in $v4; do iptables -w -A AFD-GW -d "$ip" -p tcp --dport 443 -j ACCEPT; done
  for ip in $v6; do ip6tables -w -A AFD-GW -d "$ip" -p tcp --dport 443 -j ACCEPT; done
  iptables -w -A AFD-GW -j REJECT; ip6tables -w -A AFD-GW -j REJECT
  # afd-web: replies only
  for t in iptables ip6tables; do $t -w -A AFD-WEB -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT; $t -w -A AFD-WEB -j REJECT; done
  # INPUT: gateway address only via loopback
  chain iptables AFD-GWIN; iptables -w -A AFD-GWIN -d "$GW" ! -i lo -j DROP
  jump_first iptables OUTPUT AFD-OUT; jump_first ip6tables OUTPUT AFD-OUT; jump_first iptables INPUT AFD-GWIN
  echo "afd-firewall: $mode; inference v4=[$(echo $v4)] v6=[$(echo $v6)] resolvers=[$(echo $RESOLVERS4 $RESOLVERS6)]"
}

case "${1:-}" in
  apply) apply locked ;;
  build-window) apply build ;;
  remove) for t in iptables ip6tables; do
            unjump $t OUTPUT AFD-OUT     # removes the jump and AFD-OUT; the per-user chains are then unreferenced
            for c in AFD-ORCH AFD-GW AFD-WEB; do $t -w -n -L $c >/dev/null 2>&1 && { $t -w -F $c; $t -w -X $c; }; done
          done
          unjump iptables INPUT AFD-GWIN ;;
  show) for t in iptables ip6tables; do echo "== $t"; for c in AFD-OUT AFD-ORCH AFD-GW AFD-WEB; do $t -w -S $c 2>/dev/null; done; done; iptables -w -S AFD-GWIN 2>/dev/null ;;
  *) echo "usage: $0 apply|build-window|remove|show" >&2; exit 64 ;;
esac
