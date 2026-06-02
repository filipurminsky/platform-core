FROM python:3.12-slim AS base

# uv: copy the static binary from the official image (version-pinned; Renovate
# tracks this tag via the dockerfile manager).
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# confluent-kafka needs librdkafka
RUN apt-get update && apt-get install -y --no-install-recommends \
    librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

# Install runtime deps in a cached layer from the lockfile only.
# --frozen fails the build if uv.lock is stale vs pyproject.toml.
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

# TTS_BACKEND=stub by default; prod image overrides to kokoro.
#
# --- KOKORO PROD IMAGE NOTES ---
# The prod CPU image is built from this base with an additional layer that:
#   1. Sets TTS_BACKEND=kokoro
#   2. Installs kokoro + torch (CPU-only wheel) via:
#        RUN pip install kokoro torch --index-url https://download.pytorch.org/whl/cpu
#   3. Bakes the Kokoro model + voice packs by running:
#        RUN python -c "import kokoro; kokoro.KPipeline(lang_code='en-us')"
#      (downloads and caches model weights into the image layer)
# This keeps the base image light + testable without ML deps.
# ----------------------------------

CMD ["python", "main.py"]
