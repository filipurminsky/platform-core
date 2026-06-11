"""Typed, validated configuration loaded from the environment (pydantic-settings).

All env parsing lives in the `Settings` model — one place, validated and
fail-fast at import. The UPPER_CASE module constants below are derived from the
validated `settings` singleton and are what the rest of the app imports.
"""

import sys

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """worker-service configuration (safe defaults for local dev)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    bootstrap_servers: str = Field(default="localhost:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    topic_jobs: str = Field(default="jobs", alias="KAFKA_TOPIC")
    topic_dlq: str = Field(default="jobs-dlq", alias="KAFKA_DLQ_TOPIC")
    consumer_group: str = Field(default="worker-service", alias="KAFKA_CONSUMER_GROUP")
    max_retries: int = Field(default=3, alias="MAX_RETRIES")
    metrics_port: int = Field(default=9090, alias="METRICS_PORT")

    # SASL/SCRAM — injected by Strimzi KafkaUser secret in prod; empty → plaintext for local dev
    sasl_username: str = Field(default="", alias="KAFKA_SASL_USERNAME")
    sasl_password: SecretStr = Field(default=SecretStr(""), alias="KAFKA_SASL_PASSWORD")
    security_protocol: str | None = Field(default=None, alias="KAFKA_SECURITY_PROTOCOL")
    ssl_ca_location: str = Field(default="", alias="KAFKA_SSL_CA_LOCATION")

    @model_validator(mode="after")
    def _default_security_protocol(self) -> "Settings":
        # Default to SASL_SSL only when credentials are present, else PLAINTEXT.
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
TOPIC_JOBS = settings.topic_jobs
TOPIC_DLQ = settings.topic_dlq
CONSUMER_GROUP = settings.consumer_group
MAX_RETRIES = settings.max_retries
METRICS_PORT = settings.metrics_port
SASL_USERNAME = settings.sasl_username
SASL_PASSWORD = settings.sasl_password.get_secret_value()
SECURITY_PROTOCOL = settings.security_protocol
SSL_CA_LOCATION = settings.ssl_ca_location
