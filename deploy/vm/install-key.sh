#!/bin/bash
# Install the Vultr Serverless Inference key as /etc/afd/vultr_key (root:root 0400). Only afd-gateway.service receives it,
# via systemd LoadCredential. Reads one line from stdin (hidden when interactive); never echoes, logs or prints the key.
# Accepts a raw key or a KEY=VALUE line.  Usage: sudo install-key.sh   (then paste the key and press Enter)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
if [ -t 0 ]; then printf 'Vultr inference key (input hidden): ' >&2; IFS= read -rs line; echo >&2; else IFS= read -r line; fi
line=${line#*=}; line=${line%\"}; line=${line#\"}; line=${line%\'}; line=${line#\'}
[ ${#line} -ge 20 ] || { echo "that does not look like a key (too short); nothing written" >&2; exit 1; }
umask 077; tmp=$(mktemp /etc/afd/.vultr_key.XXXXXX)
printf '%s\n' "$line" > "$tmp"; unset line
chown root:root "$tmp"; chmod 0400 "$tmp"; mv -f "$tmp" /etc/afd/vultr_key
systemctl restart afd-gateway.service
systemctl is-active --quiet afd-gateway.service && echo "key installed (root:root 0400); afd-gateway.service active" || { echo "gateway failed to start: journalctl -u afd-gateway" >&2; exit 1; }
