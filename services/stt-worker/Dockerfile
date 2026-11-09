# syntax=docker/dockerfile:1
# Build context: services/   →   docker build -f stt-worker/Dockerfile services/
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
COPY stt-worker/pyproject.toml stt-worker/uv.lock ./
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
COPY stt-worker/main.py ./
COPY stt-worker/app ./app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 1000

# Metrics + probes
EXPOSE 9090

CMD ["python", "main.py"]

# ---------------------------------------------------------------------------
# GPU / NeMo image (NOT built by CI — requires `--target nemo` and a separate
# pipeline, likely a GPU/self-hosted runner: the image is multi-gigabyte).
# docker-build.yaml builds the default (runtime) target only and every k8s
# overlay sets STT_BACKEND=stub to match. Never push it as the default tag.
#
#   docker build -f stt-worker/Dockerfile --target nemo \
#     --build-arg MODEL_ID=nvidia/parakeet-rnnt-0.6b -t stt-worker:nemo services/
#
# Requirements of app/backends.py's nemo path:
#   - ffmpeg binary on PATH (decodes wav/mp3/ogg/flac/m4a → 16 kHz mono PCM;
#     override with FFMPEG_BINARY)
#   - nemo_toolkit[asr] 2.x + CUDA torch + numpy in /app/.venv
#   - model weights baked at NEMO_MODEL_PATH (default /model-cache/parakeet-rnnt-0.6b)
# ---------------------------------------------------------------------------
# FROM builder AS nemo-deps
# # NeMo + CUDA torch; versions pinned by the ML platform team.
# RUN uv pip install --python /app/.venv/bin/python \
#       "nemo_toolkit[asr]==2.*" torch numpy \
#       --extra-index-url https://download.pytorch.org/whl/cu121
#
# FROM runtime AS nemo
# ARG MODEL_ID=nvidia/parakeet-rnnt-0.6b
# USER root
# RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
#     && rm -rf /var/lib/apt/lists/*
# COPY --from=nemo-deps /app/.venv /app/.venv
# # Pre-download the model so cold starts need no network (air-gapped clusters).
# RUN MODEL_ID=${MODEL_ID} python -c "import os; \
#     from nemo.collections.asr.models import ASRModel; \
#     ASRModel.from_pretrained(os.environ['MODEL_ID']).save_to('/model-cache/parakeet-rnnt-0.6b')"
# ENV NEMO_MODEL_PATH=/model-cache/parakeet-rnnt-0.6b \
#     STT_BACKEND=nemo
# USER 1000
# CMD ["python", "main.py"]
