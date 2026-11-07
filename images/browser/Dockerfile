# Agent Fire Drill isolated headless Chromium (CDP). No secrets, non-root; network policy is applied by the orchestrator.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium socat fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* && chromium --version > /chromium-version.txt
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
USER 65534:65534
EXPOSE 9222
ENTRYPOINT ["/entrypoint.sh"]
