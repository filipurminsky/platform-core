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

# Allow the full gateway retry budget (matches max.poll.interval.ms=480000).
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
