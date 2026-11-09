# syntax=docker/dockerfile:1
#
# Build context is services/ (shared with every service):
#   docker build -f services/llm-gateway/Dockerfile services/
#
# Base image is pinned by digest for reproducible, tamper-evident builds;
# Renovate (dockerfile manager) bumps tag + digest together.
ARG PYTHON_IMAGE=python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

# ---------------------------------------------------------------------------
# Builder: resolve the locked deps into a venv. uv exists only in this stage.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

WORKDIR /app
# Use the image's interpreter (never download one) and copy files out of the
# cache so the venv is self-contained when copied to the runtime stage.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Runtime deps only, from the lockfile only. --frozen fails the build if
# uv.lock is stale vs pyproject.toml.
COPY llm-gateway/pyproject.toml llm-gateway/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Runtime: interpreter + venv + app code. No uv, no build tooling.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Fixed numeric UID so Kubernetes runAsNonRoot can verify it without a lookup.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /app/.venv /app/.venv
COPY llm-gateway/main.py ./
COPY llm-gateway/app ./app

USER 10001

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
