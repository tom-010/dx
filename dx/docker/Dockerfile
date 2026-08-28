# syntax=docker/dockerfile:1
# Production image: gunicorn serves the Django API and the built React SPA (WhiteNoise). The same
# image runs the Celery worker and beat (different command). Build context is the repo root:
#   docker build -f docker/Dockerfile -t dx-app .        (or ./scripts/build.sh)
# Run it with docker/docker-compose.prod.yml (Caddy for TLS, Postgres, Valkey, RustFS) or on any
# platform that provides the environment from config/env.py. docker/entrypoint.sh refuses to
# start with an unsafe configuration (`manage.py check --deploy`).

ARG PYTHON_VERSION=3.14
ARG NODE_VERSION=24
ARG UV_VERSION=0.9.18

# Pinned uv binary (COPY --from cannot expand build args itself, hence the stage).
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ---- Stage 1: frontend build (pnpm + Vite) ------------------------------------------------
FROM node:${NODE_VERSION}-alpine AS frontend
WORKDIR /src/frontend
RUN npm install -g pnpm@11
# Dependencies first: this layer is cached until the lockfile changes.
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/pnpm-store \
    pnpm config set store-dir /pnpm-store && pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# ---- Stage 2: Python dependencies (uv) ---------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS backend-deps
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---- Stage 3: runtime --------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS app
ARG APP_VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/backend/.venv/bin:$PATH" \
    DEBUG=false \
    APP_VERSION=${APP_VERSION}
# Unprivileged runtime user. The code stays root-owned (read-only for the app); only media/
# (MEDIA_STORAGE=local) is writable.
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app/backend
COPY --from=backend-deps /app/backend/.venv ./.venv
COPY backend/ ./
COPY --from=frontend /src/frontend/dist /app/frontend/dist
COPY --chmod=755 docker/entrypoint.sh /app/entrypoint.sh
COPY docker/gunicorn.conf.py /app/gunicorn.conf.py
# collectstatic only needs importable settings; the placeholder key never serves a request.
RUN SECRET_KEY=build-only-placeholder-never-used-to-serve-requests \
    python manage.py collectstatic --noinput \
    && python -m compileall -q apps config manage.py \
    && mkdir -p media && chown app:app media
USER app
EXPOSE 8000
# Readiness (database migrated, broker, buckets — apps/core/health.py), asked over loopback:
# loopback is always in ALLOWED_HOSTS and the probes are exempt from the https redirect.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=8)"
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "--config", "/app/gunicorn.conf.py", "config.wsgi:application"]
LABEL org.opencontainers.image.title="dx" \
      org.opencontainers.image.version="${APP_VERSION}"
