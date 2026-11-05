"""Typed, validated configuration (pydantic-settings, fail-fast at import).

Shared Kafka / retry / S3 / Redis fields come from
`platform_common.settings.AudioWorkerSettings`; only service-specific defaults
are declared here.
"""

import sys
from typing import Literal

from platform_common.settings import AudioWorkerSettings
from pydantic import Field, ValidationError

SERVICE_NAME = "stt-worker"
STAGE = "stt"

# GPU inference can take minutes per attempt; 20 min without a heartbeat means
# the loop is wedged (back-off sleeps heartbeat, so they never trip this).
STALE_SECONDS = 1200


class Settings(AudioWorkerSettings):
    """stt-worker configuration (safe defaults for local dev)."""

    topic_in: str = Field(default="audio.jobs", alias="KAFKA_TOPIC_IN")
    topic_out: str = Field(default="audio.transcripts", alias="KAFKA_TOPIC_OUT")
    topic_dlq: str = Field(default="audio.stt-dlq", alias="KAFKA_DLQ_TOPIC")
    consumer_group: str = Field(default="stt-worker", alias="KAFKA_CONSUMER_GROUP")

    # Real model inference can take minutes: 300 s retry budget + 300 s for the
    # slowest attempt + margin fit inside a 15 min poll interval.
    max_poll_interval_ms: int = Field(
        default=900_000, ge=10_000, alias="KAFKA_MAX_POLL_INTERVAL_MS"
    )
    retry_budget_seconds: float = Field(default=300.0, gt=0, alias="RETRY_BUDGET_SECONDS")
    attempt_timeout_seconds: float = Field(default=300.0, gt=0, alias="ATTEMPT_TIMEOUT_SECONDS")

    # STT backend — a typo must fail at startup, not silently fall back to stub.
    stt_backend: Literal["stub", "nemo"] = Field(default="stub", alias="STT_BACKEND")
    nemo_model_path: str = Field(default="/model-cache/parakeet-rnnt-0.6b", alias="NEMO_MODEL_PATH")
    ffmpeg_binary: str = Field(default="ffmpeg", alias="FFMPEG_BINARY")


try:
    settings = Settings()
except ValidationError as exc:  # pragma: no cover - fail fast at startup
    print(f"Configuration error:\n{exc}", file=sys.stderr)
    sys.exit(1)


# Backward-compatible constants (single source: the validated `settings`).
BOOTSTRAP_SERVERS = settings.bootstrap_servers
TOPIC_IN = settings.topic_in
TOPIC_OUT = settings.topic_out
TOPIC_DLQ = settings.topic_dlq
CONSUMER_GROUP = settings.consumer_group
MAX_RETRIES = settings.max_retries
METRICS_PORT = settings.metrics_port
STT_BACKEND = settings.stt_backend
S3_BUCKET = settings.s3_bucket
JOB_STATE_TTL_SECONDS = settings.job_state_ttl_seconds
