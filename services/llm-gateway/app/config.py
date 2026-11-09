"""Typed, validated configuration loaded from the environment (pydantic-settings).

All env parsing lives in the `Settings` model — one place, validated and
fail-fast at import. The UPPER_CASE module constants below are derived from the
validated `settings` singleton and are what the rest of the app imports.
"""

import ipaddress
import sys
from typing import Annotated

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    """llm-gateway configuration (safe defaults for local dev)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    environment: str = Field(default="dev", alias="ENVIRONMENT")

    vllm_base_url: str = Field(default="http://vllm-inference:8000", alias="VLLM_BASE_URL")
    rate_limit_rpm: int = Field(default=60, ge=1, alias="RATE_LIMIT_RPM")
    request_timeout: float = Field(default=120.0, gt=0, alias="REQUEST_TIMEOUT_SECONDS")
    # When set, the rate limiter is shared across replicas via Redis (prod). Left
    # empty in dev (single replica), where the in-process limiter is sufficient.
    redis_url: str = Field(default="", alias="REDIS_URL")
    redis_password: SecretStr = Field(default=SecretStr(""), alias="REDIS_PASSWORD")

    # CSV of accepted bearer API keys for /v1/*. Empty → auth disabled (dev only;
    # refused at startup when ENVIRONMENT is prod/production, see below).
    gateway_api_keys: SecretStr = Field(default=SecretStr(""), alias="GATEWAY_API_KEYS")

    # CSV of CIDRs whose X-Forwarded-For is honoured (e.g. the ingress controller
    # pod CIDR). Empty → XFF is ignored and the direct peer address is used.
    # NoDecode: the env value is a CSV string, not JSON.
    trusted_proxy_cidrs: Annotated[list[IPNetwork], NoDecode] = Field(
        default_factory=list, alias="TRUSTED_PROXY_CIDRS"
    )

    # Retry-After (seconds) sent with the 503 returned when the upstream cannot be
    # connected to — typically vLLM scaled to zero while KEDA brings a pod up.
    upstream_unavailable_retry_after_seconds: int = Field(
        default=30, ge=1, alias="UPSTREAM_UNAVAILABLE_RETRY_AFTER_SECONDS"
    )
    # Hard cap on the proxied request body; larger requests get 413.
    max_request_bytes: int = Field(default=1024 * 1024, gt=0, alias="MAX_REQUEST_BYTES")

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def _parse_cidrs(cls, value):
        if isinstance(value, str):
            value = _split_csv(value)
        # strict=False: accept host bits set (e.g. "10.0.0.1/8") as the network.
        return [ipaddress.ip_network(str(item), strict=False) for item in value]

    @property
    def api_keys(self) -> tuple[str, ...]:
        return tuple(_split_csv(self.gateway_api_keys.get_secret_value()))

    @model_validator(mode="after")
    def _require_api_keys_in_prod(self) -> Settings:
        # Fail closed: an empty GATEWAY_API_KEYS disables auth, which is a dev
        # convenience only. Refuse to start in prod rather than serve /v1/* open.
        if self.environment.lower() in ("prod", "production") and not self.api_keys:
            raise ValueError(
                "GATEWAY_API_KEYS must be set when ENVIRONMENT=prod (auth would be disabled)"
            )
        return self


try:
    settings = Settings()
except ValidationError as exc:  # pragma: no cover - fail fast at startup
    # Print field + message only: str(exc) would echo `input_value`, which for a
    # model-level error is the whole env-derived input (passwords included).
    print("Configuration error:", file=sys.stderr)
    for err in exc.errors(include_url=False, include_input=False):
        loc = ".".join(str(part) for part in err["loc"]) or "settings"
        print(f"  {loc}: {err['msg']}", file=sys.stderr)
    sys.exit(1)


# Backward-compatible constants (single source: the validated `settings`).
VLLM_BASE_URL = settings.vllm_base_url
RATE_LIMIT_RPM = settings.rate_limit_rpm
REQUEST_TIMEOUT = settings.request_timeout
REDIS_URL = settings.redis_url
REDIS_PASSWORD = settings.redis_password.get_secret_value()
API_KEYS = settings.api_keys
TRUSTED_PROXY_CIDRS = tuple(settings.trusted_proxy_cidrs)
UPSTREAM_UNAVAILABLE_RETRY_AFTER = settings.upstream_unavailable_retry_after_seconds
MAX_REQUEST_BYTES = settings.max_request_bytes
