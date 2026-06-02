"""Typed, validated configuration loaded from the environment (pydantic-settings).

All env parsing lives in the `Settings` model — one place, validated and
fail-fast at import. The UPPER_CASE module constants below are derived from the
validated `settings` singleton and are what the rest of the app imports.
"""

import sys

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """tts-worker configuration (safe defaults for local dev)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    bootstrap_servers: str = Field(default="localhost:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    topic_in: str = Field(default="audio.summaries", alias="KAFKA_TOPIC_IN")
    topic_out: str = Field(default="audio.results", alias="KAFKA_TOPIC_OUT")
    topic_dlq: str = Field(default="audio.tts-dlq", alias="KAFKA_DLQ_TOPIC")
    consumer_group: str = Field(default="tts-worker", alias="KAFKA_CONSUMER_GROUP")
    max_retries: int = Field(default=3, alias="MAX_RETRIES")
    metrics_port: int = Field(default=9090, alias="METRICS_PORT")

    # SASL/SCRAM — injected by Strimzi KafkaUser secret in prod; empty → plaintext for local dev
    sasl_username: str = Field(default="", alias="KAFKA_SASL_USERNAME")
    sasl_password: str = Field(default="", alias="KAFKA_SASL_PASSWORD")
    security_protocol: str | None = Field(default=None, alias="KAFKA_SECURITY_PROTOCOL")

    # S3 / MinIO
    s3_bucket: str = Field(default="audio-pipeline", alias="S3_BUCKET")
    s3_region: str = Field(default="eu-west-1", alias="S3_REGION")
    s3_endpoint_url: str = Field(default="", alias="S3_ENDPOINT_URL")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    job_state_ttl_seconds: int = Field(default=604800, alias="JOB_STATE_TTL_SECONDS")

    # TTS backend
    tts_backend: str = Field(default="stub", alias="TTS_BACKEND")  # stub | kokoro

    @model_validator(mode="after")
    def _default_security_protocol(self) -> "Settings":
        if self.security_protocol is None:
            self.security_protocol = "SASL_SSL" if self.sasl_username else "PLAINTEXT"
        return self


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
SASL_USERNAME = settings.sasl_username
SASL_PASSWORD = settings.sasl_password
SECURITY_PROTOCOL = settings.security_protocol
S3_BUCKET = settings.s3_bucket
S3_REGION = settings.s3_region
S3_ENDPOINT_URL = settings.s3_endpoint_url
REDIS_URL = settings.redis_url
JOB_STATE_TTL_SECONDS = settings.job_state_ttl_seconds
TTS_BACKEND = settings.tts_backend
