# Agent Fire Drill agent worker (untrusted): browser-use episode against an isolated browser; reaches only the gateway.
FROM python:3.11.18-slim-trixie@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock && pip check
COPY worker.py /app/worker.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp ANONYMIZED_TELEMETRY=false BROWSER_USE_CLOUD_SYNC=false
USER 10002:10002
CMD ["python", "/app/worker.py"]
