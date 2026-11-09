FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

# uv: copy the static binary from the official image.
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Non-root runtime user
RUN useradd --create-home --uid 10001 appuser

# Install runtime deps in a cached layer (no dev group). `uv lock` runs on first
# build to produce uv.lock — commit it to your service repo for reproducible builds.
COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

COPY app/ ./app/

USER 10001
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
