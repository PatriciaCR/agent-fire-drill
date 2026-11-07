#!/bin/sh
# Chromium binds CDP to loopback inside the container; socat exposes it on the container's internal-network address only.
set -e
export HOME=/tmp/home; mkdir -p $HOME
chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --no-first-run --no-default-browser-check \
  --disable-background-networking --disable-sync --disable-component-update --metrics-recording-only --password-store=basic \
  --user-data-dir=/tmp/profile --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 --window-size=1280,720 about:blank &
for i in $(seq 1 50); do (echo > /dev/tcp/127.0.0.1/9223) 2>/dev/null && break; sleep 0.2; done 2>/dev/null || true
exec socat TCP-LISTEN:9222,fork,reuseaddr TCP:127.0.0.1:9223
