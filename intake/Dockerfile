# syntax=docker/dockerfile:1.7
#
# Single-container image. Python + gunicorn + SQLite + whitenoise. No node, no
# Redis, no worker.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv standalone.
COPY --from=ghcr.io/astral-sh/uv:0.5.7 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies from the committed lockfile — reproducible builds, and
# this layer only invalidates when pyproject/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy the project + asset build scripts.
COPY src/ ./src/
COPY scripts/ ./scripts/
COPY README.md ./

# Build the Tailwind CSS and vendor the JS deps so the image is self-contained.
RUN bash scripts/fetch_vendor.sh \
    && bash scripts/build_css.sh \
    && rm -rf .tools

ENV PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.prod \
    PYTHONPATH=/app/src \
    INTAKE_DB_PATH=/data/db.sqlite3 \
    INTAKE_DEBUG=0

# Volume target for the SQLite file.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Entrypoint: migrate (idempotent, also bootstraps the admin user), collect
# static, then run gunicorn.
COPY <<'EOF' /entrypoint.sh
#!/usr/bin/env bash
set -euo pipefail
python /app/src/manage.py migrate --noinput
# input.css is the Tailwind *source*; it contains `@import "tailwindcss"`,
# which whitenoise's manifest storage cannot resolve. Only tailwind.css (the
# built output) is served.
python /app/src/manage.py collectstatic --noinput --ignore input.css
# --timeout 600: imports run inline in the request (by design, no Celery) and a
# full Trakt history pull can take minutes. The default 30s would SIGKILL the
# worker mid-import and strand the ImportJob in `running`.
exec gunicorn config.wsgi:application \
    --chdir /app/src \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --threads 4 \
    --timeout 600 \
    --access-logfile - \
    --error-logfile -
EOF
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
