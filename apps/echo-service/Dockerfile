FROM python:3.12-slim AS base

WORKDIR /app

# Install dependencies in a separate layer for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY main.py .

# Non-root user
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

EXPOSE 8080
ENV PORT=8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
