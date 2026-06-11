"""Redis job-state store (§5).

State is a single JSON document per job under `job:<job_id>`. The mark_* helpers
read-modify-write that document with the configured TTL, and never raise — Redis
hiccups are logged, not fatal to processing.
"""

import json
from datetime import UTC, datetime

import redis as redis_lib

from app import config
from app.observability import log


def make_redis_client():
    return redis_lib.from_url(
        config.REDIS_URL, password=config.REDIS_PASSWORD or None, decode_responses=True
    )


def _get_state(r, job_id: str) -> dict:
    try:
        raw = r.get(f"job:{job_id}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _set_state(r, job_id: str, state: dict) -> None:
    try:
        r.set(f"job:{job_id}", json.dumps(state), ex=config.JOB_STATE_TTL)
    except Exception as exc:
        log.warning("redis_set_failed", job_id=job_id, error=str(exc))


def mark_summarizing(r, job_id: str) -> None:
    state = _get_state(r, job_id)
    state["status"] = "summarizing"
    state["stage"] = "llm"
    state["updated_at"] = datetime.now(UTC).isoformat()
    _set_state(r, job_id, state)


def mark_done(r, job_id: str, summary_key: str) -> None:
    state = _get_state(r, job_id)
    # "summarizing" here means "llm stage complete, waiting for tts" — tts-worker
    # will transition to synthesizing/done. The status contract has no "summarized"
    # state so we stay on "summarizing" until tts picks it up.
    state["status"] = "summarizing"
    state["stage"] = "llm"
    state["updated_at"] = datetime.now(UTC).isoformat()
    keys = state.get("keys", {})
    keys["summary"] = summary_key
    state["keys"] = keys
    _set_state(r, job_id, state)


def mark_failed(r, job_id: str, error_msg: str) -> None:
    state = _get_state(r, job_id)
    state["status"] = "failed"
    state["stage"] = "llm"
    state["updated_at"] = datetime.now(UTC).isoformat()
    state["error"] = {
        "stage": "llm",
        "message": error_msg,
        "dlq_topic": config.TOPIC_DLQ,
    }
    _set_state(r, job_id, state)
