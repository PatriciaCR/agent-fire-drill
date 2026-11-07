# Diagnostic only (not part of the product): Playwright Python client, connects over CDP to the isolated browser.
FROM python:3.11.18-slim-trixie@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79
RUN pip install --no-cache-dir playwright==1.55.0
COPY repro.py /app/repro.py
USER 10002:10002
ENV HOME=/tmp
CMD ["python", "/app/repro.py"]
