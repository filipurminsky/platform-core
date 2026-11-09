# syntax=docker/dockerfile:1
# Build context: services/   →   docker build -f llm-worker/Dockerfile services/
# (the shared services/platform-common library must be inside the context).

# ── builder: resolve the locked venv with uv ─────────────────────────────────
FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS builder

# uv: static binary from the official image (Renovate tracks tag + digest).
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.14

WORKDIR /app

# Third-party deps first (cached layer): lockfile only, no app code, no dev
# group. --frozen fails the build if uv.lock is stale vs pyproject.toml.
# confluent-kafka ships manylinux wheels with librdkafka bundled — no apt deps.
COPY platform-common/pyproject.toml /platform-common/pyproject.toml
COPY llm-worker/pyproject.toml llm-worker/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-install-package platform-common

# Then the shared library, installed NON-editable so the venv is self-contained.
COPY platform-common/ /platform-common/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable

# ── runtime: no uv, no build tooling ─────────────────────────────────────────
FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f AS runtime

RUN useradd --uid 1000 --no-create-home --shell /bin/false appuser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY llm-worker/main.py ./
COPY llm-worker/app ./app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 1000

# Metrics + probes
EXPOSE 9090

CMD ["python", "main.py"]
