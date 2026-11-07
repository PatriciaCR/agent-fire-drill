# Agent Fire Drill inference gateway: the only container that can read the Vultr key. Standard library only.
FROM python:3.11.18-slim-trixie@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79
COPY gateway.py /app/gateway.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8080
CMD ["python", "/app/gateway.py"]
