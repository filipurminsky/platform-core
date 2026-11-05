"""Typed, validated configuration (pydantic-settings, fail-fast at import).

Shared Kafka / retry / metrics fields come from `platform_common.settings`;
only service-specific defaults are declared here.
"""

import sys

from platform_common.settings import WorkerSettings
from pydantic import Field, ValidationError

SERVICE_NAME = "worker-service"

# The handlers run in milliseconds; 120 s without a poll heartbeat means the
# loop is wedged (back-off sleeps heartbeat, so they never trip this).
STALE_SECONDS = 120


class Settings(WorkerSettings):
    """worker-service configuration (safe defaults for local dev)."""

    topic_jobs: str = Field(default="jobs", alias="KAFKA_TOPIC")
    topic_dlq: str = Field(default="jobs-dlq", alias="KAFKA_DLQ_TOPIC")
    consumer_group: str = Field(default="worker-service", alias="KAFKA_CONSUMER_GROUP")
    # Handlers are fast: a 60 s retry budget + 10 s attempt fit the 300 s poll interval.
    retry_budget_seconds: float = Field(default=60.0, gt=0, alias="RETRY_BUDGET_SECONDS")
    attempt_timeout_seconds: float = Field(default=10.0, gt=0, alias="ATTEMPT_TIMEOUT_SECONDS")
    # In-memory dedup window (job ids remembered per pod; see main.py).
    dedup_cache_size: int = Field(default=10_000, ge=0, alias="DEDUP_CACHE_SIZE")


try:
    settings = Settings()
except ValidationError as exc:  # pragma: no cover - fail fast at startup
    print(f"Configuration error:\n{exc}", file=sys.stderr)
    sys.exit(1)


# Backward-compatible constants (single source: the validated `settings`).
BOOTSTRAP_SERVERS = settings.bootstrap_servers
TOPIC_JOBS = settings.topic_jobs
TOPIC_DLQ = settings.topic_dlq
CONSUMER_GROUP = settings.consumer_group
MAX_RETRIES = settings.max_retries
METRICS_PORT = settings.metrics_port
DEDUP_CACHE_SIZE = settings.dedup_cache_size
