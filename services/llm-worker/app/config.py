"""Typed, validated configuration (pydantic-settings, fail-fast at import).

Shared Kafka / retry / S3 / Redis fields come from
`platform_common.settings.AudioWorkerSettings`; only service-specific settings
are declared here.

Timing budget (all per message, validated against max.poll.interval.ms):
  LLM_COLD_START_BUDGET_SECONDS (600) — total wall-clock spent waiting/retrying
      while the gateway answers 429/502/503/504 or refuses connections (vLLM
      scaling from zero). Honours the gateway's Retry-After.
  LLM_ATTEMPT_BUDGET_SECONDS (240) — one full attempt (all map-reduce calls).
  KAFKA_MAX_POLL_INTERVAL_MS (1 200 000) > 600 + 240 + 60 s margin.
"""

import sys

from platform_common.settings import AudioWorkerSettings
from pydantic import Field, SecretStr, ValidationError

SERVICE_NAME = "llm-worker"
STAGE = "llm"

# A single gateway call is bounded by LLM_TIMEOUT_SECONDS (120 s) and every
# retry wait heartbeats, so 600 s without a heartbeat means a wedged loop.
STALE_SECONDS = 600


class Settings(AudioWorkerSettings):
    """llm-worker configuration (safe defaults for local dev)."""

    topic_in: str = Field(default="audio.transcripts", alias="KAFKA_TOPIC_IN")
    topic_out: str = Field(default="audio.summaries", alias="KAFKA_TOPIC_OUT")
    topic_dlq: str = Field(default="audio.llm-dlq", alias="KAFKA_DLQ_TOPIC")
    consumer_group: str = Field(default="llm-worker", alias="KAFKA_CONSUMER_GROUP")

    max_poll_interval_ms: int = Field(
        default=1_200_000, ge=10_000, alias="KAFKA_MAX_POLL_INTERVAL_MS"
    )
    # For this worker the retry budget IS the cold-start budget.
    retry_budget_seconds: float = Field(default=600.0, gt=0, alias="LLM_COLD_START_BUDGET_SECONDS")
    attempt_timeout_seconds: float = Field(default=240.0, gt=0, alias="LLM_ATTEMPT_BUDGET_SECONDS")

    # LLM gateway — never talk to vLLM directly (contract §6).
    llm_gateway_url: str = Field(
        default="http://llm-gateway.apps.svc.cluster.local:80", alias="LLM_GATEWAY_URL"
    )
    # Sent as `Authorization: Bearer <key>`; optional (dev gateway may run without auth).
    llm_gateway_api_key: SecretStr | None = Field(default=None, alias="LLM_GATEWAY_API_KEY")
    llm_model: str = Field(default="TinyLlama/TinyLlama-1.1B-Chat-v1.0", alias="LLM_MODEL")
    llm_timeout: float = Field(default=120.0, gt=0, alias="LLM_TIMEOUT_SECONDS")
    llm_max_tokens: int = Field(default=512, gt=0, alias="LLM_MAX_TOKENS")
    llm_chunk_max_tokens: int = Field(default=256, gt=0, alias="LLM_CHUNK_MAX_TOKENS")

    # Context-window guard. TinyLlama has a 2 048-token context: 512 output +
    # prompt overhead leaves ~1 300 input tokens ≈ 4 000 chars (≈ 3-4 chars/token).
    # Longer transcripts are summarised map-reduce in chunks of this size.
    llm_max_input_chars: int = Field(default=4000, ge=500, alias="LLM_MAX_INPUT_CHARS")
    # Map-phase cap; anything beyond max_chunks × max_input_chars is truncated
    # (logged + llm_transcript_truncated_total).
    llm_max_chunks: int = Field(default=4, ge=1, alias="LLM_MAX_CHUNKS")


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
LLM_GATEWAY_URL = settings.llm_gateway_url
LLM_MODEL = settings.llm_model
LLM_TIMEOUT = settings.llm_timeout
S3_BUCKET = settings.s3_bucket
JOB_STATE_TTL = settings.job_state_ttl_seconds
