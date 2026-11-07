# Agent Fire Drill synthetic site: BreakingWeb (pinned, unmodified; Apache-2.0) patient_portal + our outer wrapper.
# Build context: build/site-ctx (assembled by `make site`): ./breakingweb = pristine BreakingWeb export, ./afd_site.py, ./requirements.lock
FROM node:24-slim AS frontend
WORKDIR /src
COPY breakingweb/breakingweb /src/breakingweb
RUN corepack enable && corepack prepare pnpm@10.15.0 --activate \
 && cd /src/breakingweb/environments && pnpm install --frozen-lockfile --filter @breakingweb/patient_portal... \
 && pnpm --filter @breakingweb/patient_portal build

FROM python:3.11.18-slim-trixie@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock && pip check
WORKDIR /app
COPY breakingweb/breakingweb /app/breakingweb
COPY --from=frontend /src/breakingweb/static/envs/patient_portal /app/breakingweb/static/envs/patient_portal
RUN rm -rf /app/breakingweb/environments/*/node_modules
COPY breakingweb/LICENSE /app/BREAKINGWEB_LICENSE
COPY afd_site.py /app/afd_site.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BREAKINGWEB_AUTO_BUILD_FRONTENDS=0
USER 65534:65534
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "afd_site:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
