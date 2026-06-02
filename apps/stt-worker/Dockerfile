FROM python:3.12-slim AS base

# uv: copy the static binary from the official image (version-pinned; Renovate
# tracks this tag via the dockerfile manager).
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# confluent-kafka needs librdkafka; install it from OS packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

# Install runtime deps in a cached layer from the lockfile only (no app code,
# no dev group). --frozen fails the build if uv.lock is stale vs pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Run from the project venv.
ENV PATH="/app/.venv/bin:$PATH"

COPY main.py .
COPY app ./app

RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

# Metrics port
EXPOSE 9090

# Allow longer poll interval without pod eviction (matches max.poll.interval.ms=300000)
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]

# ---------------------------------------------------------------------------
# GPU / NeMo prod image (not built by default — requires --target nemo or a
# separate pipeline stage).  This stage bakes the Parakeet RNNT model weights
# and all heavy ML deps on top of the base runtime layer.
#
# To build:
#   docker build --target nemo -t stt-worker:nemo \
#     --build-arg MODEL_ID=nvidia/parakeet-rnnt-0.6b .
#
# The prod CI pipeline (docker-build.yaml) selects this target when
# STT_BACKEND=nemo.  The resulting image is multi-gigabyte; do NOT push it as
# the default :latest tag.
# ---------------------------------------------------------------------------
# FROM base AS nemo
#
# ARG MODEL_ID=nvidia/parakeet-rnnt-0.6b
#
# USER root
# # NeMo + torch for CUDA 12.1; versions pinned by the ML platform team.
# RUN pip install --no-cache-dir \
#     "nemo_toolkit[asr]==2.0.*" \
#     "torch==2.2.*+cu121" \
#     --extra-index-url https://download.pytorch.org/whl/cu121
#
# # Pre-download the Parakeet model into the image so cold starts don't need
# # network access (important for air-gapped clusters).
# RUN python - <<'EOF'
# from nemo.collections.asr.models import EncDecRNNTBPEModel
# import os
# model = EncDecRNNTBPEModel.from_pretrained(os.environ["MODEL_ID"])
# model.save_to("/model-cache/parakeet-rnnt-0.6b")
# EOF
#
# ENV NEMO_MODEL_PATH=/model-cache/parakeet-rnnt-0.6b
# ENV STT_BACKEND=nemo
#
# USER appuser
# CMD ["python", "main.py"]
