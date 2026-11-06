"""pydantic-settings base classes shared by every worker.

Each service subclasses these and re-declares the fields whose *defaults*
differ (topics, consumer group, timing budgets). All env parsing stays typed and
fail-fast at startup.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Safety margin (s) between the worst-case per-message processing time and
# max.poll.interval.ms, so a slow-but-healthy job never triggers a rebalance.
POLL_INTERVAL_MARGIN_SECONDS = 60


class WorkerSettings(BaseSettings):
    """Kafka + retry + metrics settings common to every consumer."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, populate_by_name=True)

    bootstrap_servers: str = Field(default="localhost:9092", alias="KAFKA_BOOTSTRAP_SERVERS")
    consumer_group: str = Field(default="worker", alias="KAFKA_CONSUMER_GROUP")
    topic_dlq: str = Field(default="dlq", alias="KAFKA_DLQ_TOPIC")
    metrics_port: int = Field(default=9090, alias="METRICS_PORT")

    # SASL/SCRAM — injected from the Strimzi KafkaUser secret in prod; empty →
    # plaintext for local dev.
    sasl_username: str = Field(default="", alias="KAFKA_SASL_USERNAME")
    sasl_password: SecretStr = Field(default=SecretStr(""), alias="KAFKA_SASL_PASSWORD")
    security_protocol: str | None = Field(default=None, alias="KAFKA_SECURITY_PROTOCOL")
    ssl_ca_location: str = Field(default="", alias="KAFKA_SSL_CA_LOCATION")

    # Consumer liveness. Must exceed the worst-case time spent on ONE message
    # (all retry back-off + the slowest single attempt) — validated below.
    max_poll_interval_ms: int = Field(
        default=300_000, ge=10_000, alias="KAFKA_MAX_POLL_INTERVAL_MS"
    )

    # Retry policy for transient failures (see platform_common.retry).
    max_retries: int = Field(default=3, ge=1, alias="MAX_RETRIES")
    retry_base_delay_seconds: float = Field(default=2.0, ge=0, alias="RETRY_BASE_DELAY_SECONDS")
    retry_max_delay_seconds: float = Field(default=30.0, ge=0, alias="RETRY_MAX_DELAY_SECONDS")
    # Upper bound on wall-clock time spent retrying ONE message (back-off
    # included); a retry whose delay would cross it dead-letters instead.
    retry_budget_seconds: float = Field(default=120.0, gt=0, alias="RETRY_BUDGET_SECONDS")
    # Worst-case duration of a single processing attempt (used only for the
    # max.poll.interval sanity check).
    attempt_timeout_seconds: float = Field(default=60.0, gt=0, alias="ATTEMPT_TIMEOUT_SECONDS")

    @model_validator(mode="after")
    def _default_security_protocol(self):
        if self.security_protocol is None:
            self.security_protocol = "SASL_SSL" if self.sasl_username else "PLAINTEXT"
        return self

    @model_validator(mode="after")
    def _poll_interval_covers_worst_case(self):
        worst = self.worst_case_message_seconds + POLL_INTERVAL_MARGIN_SECONDS
        if worst * 1000 > self.max_poll_interval_ms:
            raise ValueError(
                f"KAFKA_MAX_POLL_INTERVAL_MS={self.max_poll_interval_ms} is shorter than the "
                f"worst-case per-message time (~{worst:.0f}s = retry budget + one attempt + "
                f"{POLL_INTERVAL_MARGIN_SECONDS}s margin); the consumer would be evicted "
                "mid-job. Raise it or lower the retry budget / attempt timeout."
            )
        return self

    @property
    def worst_case_message_seconds(self) -> float:
        return self.retry_budget_seconds + self.attempt_timeout_seconds

    @property
    def max_poll_interval_seconds(self) -> float:
        return self.max_poll_interval_ms / 1000


class AudioWorkerSettings(WorkerSettings):
    """Adds the S3 (contract §4) and Redis job-state (§5) settings."""

    s3_endpoint_url: str = Field(default="", alias="S3_ENDPOINT_URL")
    s3_bucket: str = Field(default="audio-pipeline", alias="S3_BUCKET")
    s3_region: str = Field(default="eu-west-1", alias="S3_REGION")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    redis_password: SecretStr = Field(default=SecretStr(""), alias="REDIS_PASSWORD")
    job_state_ttl_seconds: int = Field(default=604_800, gt=0, alias="JOB_STATE_TTL_SECONDS")

    # Dedup claim lease. A pod that crashes mid-job holds the claim at most this
    # long before another pod may re-process the redelivered message. Defaults to
    # max.poll.interval + margin: past that the consumer has been evicted anyway.
    dedup_lease_seconds: int | None = Field(default=None, gt=0, alias="DEDUP_LEASE_SECONDS")

    @property
    def effective_dedup_lease_seconds(self) -> int:
        if self.dedup_lease_seconds is not None:
            return self.dedup_lease_seconds
        return int(self.max_poll_interval_seconds) + POLL_INTERVAL_MARGIN_SECONDS
