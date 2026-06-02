"""Typed, validated configuration loaded from the environment (pydantic-settings).

All env parsing lives in the `Settings` model — one place, validated and
fail-fast at import. The UPPER_CASE module constants below are derived from the
validated `settings` singleton and are what the rest of the app imports.
"""

import sys

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """echo-service configuration (safe defaults for local dev)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    app_version: str = Field(default="dev", alias="APP_VERSION")


try:
    settings = Settings()
except ValidationError as exc:  # pragma: no cover - fail fast at startup
    print(f"Configuration error:\n{exc}", file=sys.stderr)
    sys.exit(1)


# Backward-compatible constant (single source: the validated `settings`).
APP_VERSION = settings.app_version
