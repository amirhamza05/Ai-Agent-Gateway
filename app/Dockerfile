# syntax=docker/dockerfile:1.7
#
# GeoSWMM Gateway app image — multi-stage build.
#
# Stage 1 (builder): pull `uv`, install Python deps into a project-local venv
# at /app/.venv. Compilers and build tooling live ONLY in this stage so the
# final image stays slim and free of attack surface.
#
# Stage 2 (runtime): copy the venv and source, run as a non-root user.
# No build tools, no .env, no caches. Healthcheck hits /healthz.

# ----- Stage 1: builder -----------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Build deps for native wheels (asyncpg, argon2-cffi later, etc.). Removed
# entirely from the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv via pip — avoids pulling from ghcr.io which may be unreachable.
# Pinned for reproducibility; bump deliberately.
RUN pip install uv==0.5.11

WORKDIR /app

# Create the venv up front so the next `uv pip install` lands in it.
RUN uv venv /app/.venv

# Copy metadata + source together. We can't easily split these without a
# uv.lock — `pip install -e .` needs the package tree present for the
# hatchling build to succeed. Once a lock is committed, switch to
# `uv sync --frozen --no-dev` and you'll get a properly cached dep layer.
COPY pyproject.toml ./
COPY src ./src

RUN uv pip install --python /app/.venv/bin/python -e .

# ----- Stage 2: runtime -----------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src"

# curl is needed for the HEALTHCHECK below. Nothing else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 gateway \
    && useradd  --system --uid 10001 --gid gateway --home-dir /app --shell /usr/sbin/nologin gateway

WORKDIR /app

# Bring over the prebuilt venv and source from the builder stage. Note we
# do NOT copy any .env file — secrets are injected at runtime via env_file.
COPY --from=builder --chown=gateway:gateway /app/.venv /app/.venv
COPY --chown=gateway:gateway src /app/src
COPY --chown=gateway:gateway alembic.ini /app/alembic.ini
COPY --chown=gateway:gateway migrations /app/migrations

USER gateway

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
