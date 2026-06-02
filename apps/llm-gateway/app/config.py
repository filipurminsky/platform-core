"""Typed, validated configuration loaded from the environment (pydantic-settings).

All env parsing lives in the `Settings` model — one place, validated and
fail-fast at import. The UPPER_CASE module constants below are derived from the
validated `settings` singleton and are what the rest of the app imports.
"""

import sys

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """llm-gateway configuration (safe defaults for local dev)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    vllm_base_url: str = Field(default="http://vllm-inference:8000", alias="VLLM_BASE_URL")
    rate_limit_rpm: int = Field(default=60, alias="RATE_LIMIT_RPM")
    request_timeout: float = Field(default=120.0, alias="REQUEST_TIMEOUT_SECONDS")


try:
    settings = Settings()
except ValidationError as exc:  # pragma: no cover - fail fast at startup
    print(f"Configuration error:\n{exc}", file=sys.stderr)
    sys.exit(1)


# Backward-compatible constants (single source: the validated `settings`).
VLLM_BASE_URL = settings.vllm_base_url
RATE_LIMIT_RPM = settings.rate_limit_rpm
REQUEST_TIMEOUT = settings.request_timeout
