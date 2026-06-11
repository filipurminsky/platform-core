"""Redis job-state writes for the terminal (tts) stage.

Each helper reads the current document, merges its changes, and re-writes
with the TTL — preserving keys accumulated by earlier stages (audio,
transcript, summary). The terminal stage sets status synthesizing → done
(or failed on DLQ).

Redis errors are logged and swallowed (never raise) — job-state writes are
best-effort; a transient Redis blip should not dead-letter a job whose actual
processing succeeded.  Aligns with the stt/llm-worker policy.
"""

import json
from datetime import UTC, datetime

import redis

from app import config
from app.observability import log


def make_redis_client():
    return redis.from_url(config.REDIS_URL, decode_responses=True)


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _get_state(redis_client, job_id: str) -> dict:
    try:
        raw = redis_client.get(f"job:{job_id}")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _save_state(redis_client, job_id: str, state: dict) -> None:
    try:
        redis_client.set(f"job:{job_id}", json.dumps(state), ex=config.JOB_STATE_TTL_SECONDS)
    except Exception as exc:
        log.warning("redis_set_failed", job_id=job_id, error=str(exc))


def set_synthesizing(redis_client, job_id: str, summary_key: str) -> None:
    if not job_id:
        return
    state = _get_state(redis_client, job_id)
    state["status"] = "synthesizing"
    state["stage"] = "tts"
    state["updated_at"] = now_iso()
    keys = state.get("keys", {})
    keys["summary"] = summary_key
    state["keys"] = keys
    _save_state(redis_client, job_id, state)


def set_done(redis_client, job_id: str, summary_key: str, speech_key: str) -> None:
    if not job_id:
        return
    state = _get_state(redis_client, job_id)
    state["status"] = "done"
    state["stage"] = "tts"
    state["updated_at"] = now_iso()
    keys = state.get("keys", {})
    keys["summary"] = summary_key
    keys["speech"] = speech_key
    state["keys"] = keys
    _save_state(redis_client, job_id, state)


def set_failed(redis_client, job_id: str, error_msg: str) -> None:
    if not job_id:
        return
    state = _get_state(redis_client, job_id)
    state["status"] = "failed"
    state["stage"] = "tts"
    state["updated_at"] = now_iso()
    state["error"] = {
        "stage": "tts",
        "message": error_msg,
        "dlq_topic": config.TOPIC_DLQ,
    }
    _save_state(redis_client, job_id, state)
