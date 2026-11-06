"""Audio-pipeline event models (docs/audio-pipeline-contract.md §3) + DLQ envelopes.

Inbound events are validated with these models; anything that fails (bad JSON,
not an object, missing/invalid ``job_id``, wrong field types, a key outside its
stage prefix) is an ``InvalidMessage`` and goes to the stage DLQ — it is never
silently dropped and never processed with a guessed key.

Models ignore unknown fields (forward compatible). ``job_id`` must be a UUID
(contract §1) — it becomes part of every object-storage key.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator


def _validate_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("job_id must be a UUID string") from exc


JobId = Annotated[str, AfterValidator(_validate_uuid)]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _Event(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    job_id: JobId
    # Original job creation time, copied forward through every event (§7).
    created_at: str | None = None

    def to_json_bytes(self) -> bytes:
        return self.model_dump_json(by_alias=True).encode()


def _key_validator(field: str, prefix: str):
    """Default ``<prefix>/<job_id>`` when absent; reject keys outside the prefix."""

    def _check(self):
        value = getattr(self, field)
        if value is None:
            object.__setattr__(self, field, f"{prefix}/{self.job_id}")
        elif not value.startswith(f"{prefix}/") or len(value) <= len(prefix) + 1:
            raise ValueError(f"{field} must start with '{prefix}/'")
        return self

    return model_validator(mode="after")(_check)


class AudioJobEvent(_Event):
    """``audio.jobs`` (audio-api → stt-worker)."""

    audio_key: str | None = None
    content_type: str | None = None
    size_bytes: int | None = Field(default=None, alias="bytes", ge=0)

    _key = _key_validator("audio_key", "audio")


class TranscriptEvent(_Event):
    """``audio.transcripts`` (stt-worker → llm-worker)."""

    transcript_key: str | None = None
    language: str = "en"
    duration_s: float | None = Field(default=None, ge=0)

    _key = _key_validator("transcript_key", "transcripts")


class SummaryEvent(_Event):
    """``audio.summaries`` (llm-worker → tts-worker)."""

    summary_key: str | None = None
    action_items: list[str] = Field(default_factory=list)

    _key = _key_validator("summary_key", "summaries")


class ResultEvent(_Event):
    """``audio.results`` (tts-worker → terminal)."""

    speech_key: str | None = None
    status: Literal["done"] = "done"

    _key = _key_validator("speech_key", "speech")


class InvalidMessage(Exception):
    """An inbound message that can never be processed (→ DLQ, then commit)."""

    def __init__(self, reason: str, *, original: dict | None = None, job_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.original = original
        self.job_id = job_id


def _summarise_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('msg')}")
    return "invalid event: " + "; ".join(parts)


def _maybe_job_id(original: dict) -> str | None:
    try:
        return _validate_uuid(original.get("job_id"))
    except ValueError:
        return None


def decode_json_object(raw_value: bytes | None) -> dict:
    """Decode a Kafka value into a JSON object or raise ``InvalidMessage``."""
    if raw_value is None:
        raise InvalidMessage("empty message value (tombstone)")
    try:
        obj = json.loads(raw_value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidMessage(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise InvalidMessage(f"event must be a JSON object, got {type(obj).__name__}")
    return obj


def decode_event[M: BaseModel](raw_value: bytes | None, model: type[M]) -> M:
    """Parse + validate an inbound event, or raise ``InvalidMessage``."""
    original = decode_json_object(raw_value)
    try:
        return model.model_validate(original)
    except ValidationError as exc:
        raise InvalidMessage(
            _summarise_validation_error(exc), original=original, job_id=_maybe_job_id(original)
        ) from exc


def build_dlq_payload(
    *,
    original: dict | None,
    raw_value: bytes | None,
    error: str,
    stage: str,
    attempts: int,
    error_type: Literal["invalid", "permanent", "retries_exhausted"],
) -> bytes:
    """DLQ value per contract §3: original event + envelope.

    ``error_type`` is an additive field. When the original value was not a JSON
    object it cannot be spread, so the exact bytes are carried as ``raw_b64``.
    """
    envelope: dict[str, Any] = {
        "error": error,
        "error_type": error_type,
        "stage": stage,
        "failed_at": now_iso(),
        "attempts": attempts,
    }
    if original is not None:
        return json.dumps({**original, **envelope}).encode()
    raw_b64 = base64.b64encode(raw_value or b"").decode()
    return json.dumps({"raw_b64": raw_b64, **envelope}).encode()
