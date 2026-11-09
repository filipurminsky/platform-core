# syntax=docker/dockerfile:1
# Context must be services/ so the shared worker package is available.
FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f
COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY platform-common/ /platform-common/
WORKDIR /worker
COPY stt-worker/pyproject.toml stt-worker/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-editable
COPY stt-worker/main.py ./
COPY stt-worker/app ./app
ENV PATH="/worker/.venv/bin:$PATH"
USER 1000
ENTRYPOINT ["python", "-m", "pytest", "-p", "no:cacheprovider", "-v", "/tests/test_recovery.py", "--junitxml=/evidence/junit.xml"]
