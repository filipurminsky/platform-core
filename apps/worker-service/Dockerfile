FROM python:3.12-slim AS base

WORKDIR /app

# confluent-kafka needs librdkafka; install it from OS packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

# Metrics port
EXPOSE 9090

# Allow longer poll interval without pod eviction (matches max.poll.interval.ms=300000)
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
