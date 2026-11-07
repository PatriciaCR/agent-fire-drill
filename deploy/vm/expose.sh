#!/bin/bash
# Publish the web interface over HTTPS (Caddy reverse proxy -> 127.0.0.1:$AFD_WEB_PORT) ONLY if the latest security
# verification passed on the commit currently checked out, within the last 24 hours. Usage: sudo expose.sh [domain]
# Default domain: <ip-with-dashes>.sslip.io. `expose.sh --withdraw` stops publishing.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
ROOT=/opt/afd; . /etc/afd/afd.env
if [ "${1:-}" = --withdraw ]; then systemctl disable --now caddy 2>/dev/null || true; echo "withdrawn: caddy stopped"; exit 0; fi
python3 -I - "$AFD_ACCEPTANCE/latest.json" "$(git -c safe.directory="$ROOT" -C "$ROOT" rev-parse HEAD)" <<'PY'
import json, sys, time
p, head = sys.argv[1], sys.argv[2]
try: r = json.load(open(p))
except Exception as e: sys.exit(f"refusing to expose: no verification evidence ({e})")
bad = [k for k, v in r["checks"].items() if v.get("mandatory") and v.get("status") != "PASS"]
age = time.time() - r.get("t", 0)
if r.get("environment") != "vm": sys.exit("refusing to expose: evidence is not from the VM environment")
if r.get("commit") != head: sys.exit(f"refusing to expose: evidence is for commit {r.get('commit')}, checked out {head}")
if age > 86400: sys.exit(f"refusing to expose: evidence is {age/3600:.1f} h old (max 24 h); rerun verify.sh")
if bad or not r.get("pass"): sys.exit("refusing to expose: mandatory checks not passing: " + ", ".join(bad))
print(f"verification OK: {sum(1 for v in r['checks'].values() if v.get('mandatory'))} mandatory checks passed on {head[:12]}")
PY
[ -s /etc/afd/web_access ] || { echo "refusing to expose: no smoke-run access code; run: sudo $ROOT/deploy/vm/afdctl set-access-code" >&2; exit 1; }
DOMAIN=${1:-$(curl -fsS4 --max-time 5 https://api.ipify.org | tr . -).sslip.io}
apt-get install -y -q caddy
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	encode gzip
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		X-Frame-Options DENY
		Referrer-Policy no-referrer
		Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'"
		-Server
	}
	request_body {
		max_size 64KB
	}
	reverse_proxy $AFD_WEB_BIND:$AFD_WEB_PORT
}
CADDY
if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then ufw allow 80/tcp; ufw allow 443/tcp; fi
systemctl enable caddy; systemctl restart caddy
echo "published: https://$DOMAIN  (withdraw with: sudo $0 --withdraw)"
