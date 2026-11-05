"""Typed, validated configuration (pydantic-settings, fail-fast at import).

Shared Kafka / retry / S3 / Redis fields come from
`platform_common.settings.AudioWorkerSettings`; only service-specific defaults
are declared here.
"""

import sys
from typing import Literal

from platform_common.settings import AudioWorkerSettings
from pydantic import Field, ValidationError

SERVICE_NAME = "tts-worker"
STAGE = "tts"

# Kokoro cold start (model load) + synthesis can take minutes on CPU; 20 min
# without a heartbeat means the loop is wedged.
STALE_SECONDS = 1200


class Settings(AudioWorkerSettings):
    """tts-worker configuration (safe defaults for local dev)."""

    topic_in: str = Field(default="audio.summaries", alias="KAFKA_TOPIC_IN")
    topic_out: str = Field(default="audio.results", alias="KAFKA_TOPIC_OUT")
    topic_dlq: str = Field(default="audio.tts-dlq", alias="KAFKA_DLQ_TOPIC")
    consumer_group: str = Field(default="tts-worker", alias="KAFKA_CONSUMER_GROUP")

    max_poll_interval_ms: int = Field(
        default=900_000, ge=10_000, alias="KAFKA_MAX_POLL_INTERVAL_MS"
    )
    retry_budget_seconds: float = Field(default=300.0, gt=0, alias="RETRY_BUDGET_SECONDS")
    attempt_timeout_seconds: float = Field(default=300.0, gt=0, alias="ATTEMPT_TIMEOUT_SECONDS")

    # TTS backend — a typo must fail at startup, not silently fall back to stub.
    tts_backend: Literal["stub", "kokoro"] = Field(default="stub", alias="TTS_BACKEND")
    kokoro_lang_code: str = Field(default="a", alias="KOKORO_LANG_CODE")  # a = American English
    kokoro_voice: str = Field(default="af_heart", alias="KOKORO_VOICE")


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
S3_BUCKET = settings.s3_bucket
JOB_STATE_TTL_SECONDS = settings.job_state_ttl_seconds
TTS_BACKEND = settings.tts_backend
