FROM python:3.12-slim AS base

# uv: copy the static binary from the official image (version-pinned; Renovate
# tracks this tag via the dockerfile manager).
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install runtime deps in a cached layer from the lockfile only (no app code,
# no dev group). --frozen fails the build if uv.lock is stale vs pyproject.toml.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Run from the project venv.
ENV PATH="/app/.venv/bin:$PATH"

# Copy source
COPY main.py .

# Non-root user
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

EXPOSE 8080
ENV PORT=8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
