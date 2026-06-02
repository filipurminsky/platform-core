"""Typed, validated configuration loaded from the environment (pydantic-settings).

All env parsing lives in the `Settings` model — one place, validated and
fail-fast at import. The UPPER_CASE module constants below are derived from the
validated `settings` singleton and are what the rest of the app imports.
"""

import sys
from typing import Annotated

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_DEFAULT_CONTENT_TYPES = "audio/wav,audio/mpeg,audio/mp4,audio/x-m4a,audio/ogg,audio/flac"


class Settings(BaseSettings):
    """audio-api configuration (safe defaults for local dev)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    api_token: str = Field(default="", alias="API_TOKEN")  # empty → no auth check
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")  # 25 MB
    # NoDecode: the env value is a CSV string, not JSON — let the validator below
    # split it instead of pydantic-settings trying to JSON-decode the set.
    allowed_content_types: Annotated[set[str], NoDecode] = Field(
        default_factory=lambda: set(_DEFAULT_CONTENT_TYPES.split(",")),
        alias="ALLOWED_CONTENT_TYPES",
    )

    s3_endpoint_url: str = Field(default="", alias="S3_ENDPOINT_URL")
    s3_bucket: str = Field(default="audio-pipeline", alias="S3_BUCKET")
    s3_region: str = Field(default="eu-west-1", alias="S3_REGION")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    job_state_ttl_seconds: int = Field(default=604800, alias="JOB_STATE_TTL_SECONDS")

    kafka_bootstrap_servers: str = Field(default="localhost:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    kafka_topic_jobs: str = Field(default="audio.jobs", alias="KAFKA_TOPIC_JOBS")
    kafka_sasl_username: str = Field(default="", alias="KAFKA_SASL_USERNAME")
    kafka_sasl_password: str = Field(default="", alias="KAFKA_SASL_PASSWORD")
    kafka_security_protocol: str | None = Field(default=None, alias="KAFKA_SECURITY_PROTOCOL")

    @field_validator("allowed_content_types", mode="before")
    @classmethod
    def _split_csv(cls, value):
        # Env supplies a CSV string; in-code defaults are already a set.
        if isinstance(value, str):
            return {item.strip() for item in value.split(",") if item.strip()}
        return value

    @model_validator(mode="after")
    def _default_security_protocol(self) -> "Settings":
        if self.kafka_security_protocol is None:
            self.kafka_security_protocol = "SASL_SSL" if self.kafka_sasl_username else "PLAINTEXT"
        return self


try:
    settings = Settings()
except ValidationError as exc:  # pragma: no cover - fail fast at startup
    print(f"Configuration error:\n{exc}", file=sys.stderr)
    sys.exit(1)


# Backward-compatible constants (single source: the validated `settings`).
API_TOKEN = settings.api_token
MAX_UPLOAD_BYTES = settings.max_upload_bytes
ALLOWED_CONTENT_TYPES = settings.allowed_content_types

S3_ENDPOINT_URL = settings.s3_endpoint_url
S3_BUCKET = settings.s3_bucket
S3_REGION = settings.s3_region

REDIS_URL = settings.redis_url
JOB_STATE_TTL_SECONDS = settings.job_state_ttl_seconds

KAFKA_BOOTSTRAP_SERVERS = settings.kafka_bootstrap_servers
KAFKA_TOPIC_JOBS = settings.kafka_topic_jobs
KAFKA_SASL_USERNAME = settings.kafka_sasl_username
KAFKA_SASL_PASSWORD = settings.kafka_sasl_password
KAFKA_SECURITY_PROTOCOL = settings.kafka_security_protocol
