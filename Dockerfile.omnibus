# ── Stage 1: Build frontend ───────────────────────────────────────────────────
FROM --platform=$BUILDPLATFORM node:24-alpine AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ .
RUN npm run build && npm prune --omit=dev

# Runtime needs only the production dependencies
RUN npm prune --omit=dev

# ── Stage 2: Runtime (Python + Node + supervisord + embedded PostgreSQL) ──────
FROM python:3.12-alpine

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
ENV TZ=UTC

# nodejs runs the Astro server, su-exec drops privileges in the entrypoint,
# tzdata backs the TZ setting. PostgreSQL stays on major version 17: existing
# embedded data directories were initialised by Debian's 17.x and a different
# major would refuse to start on them. We manage our own data dir at
# /app/postgres/data.
RUN apk upgrade --no-cache && apk add --no-cache \
    "nodejs>=24.22.1" \
    postgresql17 \
    su-exec \
    tzdata

# supervisor comes from PyPI so it runs on this image's own Python (the apk
# package pulls in a second interpreter). pip is dropped afterwards since
# nothing installs packages at runtime, and it vendors libraries of its own.
RUN pip install --no-cache-dir "supervisor>=4.3" \
    && pip uninstall -y pip

# Install uv; never download a managed Python, the image's own is used
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never

# ── Backend ───────────────────────────────────────────────────────────────────
WORKDIR /app/backend

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-cache

COPY backend/ .

# The entrypoint chowns this; it must exist even when no volume is mounted over it
RUN mkdir -p /app/backend/data

# ── Frontend ──────────────────────────────────────────────────────────────────
WORKDIR /app/frontend

COPY --from=frontend-builder /app/frontend/dist ./dist
COPY --from=frontend-builder /app/frontend/node_modules ./node_modules
COPY --from=frontend-builder /app/frontend/package.json ./

# ── Entrypoint & supervisor configs ───────────────────────────────────────────
COPY entrypoint.sh /entrypoint.sh
COPY entrypoint.omnibus.sh /entrypoint.omnibus.sh
COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY supervisord.omnibus.conf /etc/supervisor/supervisord.omnibus.conf
RUN chmod +x /entrypoint.sh /entrypoint.omnibus.sh

# Persistent data lives here — mount a volume over both paths
VOLUME ["/app/backend/data", "/app/postgres/data"]

EXPOSE 7330

# Requires Docker Engine 25+ for --start-interval
HEALTHCHECK --interval=2m --timeout=2s --start-period=20s --start-interval=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:${BACKEND_PORT:-7331}/health || exit 1

ENTRYPOINT ["/entrypoint.omnibus.sh"]
