"""Redis job-state writes for the terminal (tts) stage.

Each helper writes the whole state document for `job:<job_id>` with the TTL.
The terminal stage sets status synthesizing → done (or failed on DLQ).
"""

import json
from datetime import UTC, datetime

import redis

from app import config


def make_redis_client():
    return redis.from_url(config.REDIS_URL, decode_responses=True)


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def set_synthesizing(redis_client, job_id: str, summary_key: str) -> None:
    if not job_id:
        return
    state = {
        "status": "synthesizing",
        "stage": "tts",
        "updated_at": now_iso(),
        "keys": {"summary": summary_key},
    }
    redis_client.set(f"job:{job_id}", json.dumps(state), ex=config.JOB_STATE_TTL_SECONDS)


def set_done(redis_client, job_id: str, summary_key: str, speech_key: str) -> None:
    if not job_id:
        return
    state = {
        "status": "done",
        "stage": "tts",
        "updated_at": now_iso(),
        "keys": {"summary": summary_key, "speech": speech_key},
    }
    redis_client.set(f"job:{job_id}", json.dumps(state), ex=config.JOB_STATE_TTL_SECONDS)


def set_failed(redis_client, job_id: str, error_msg: str) -> None:
    if not job_id:
        return
    state = {
        "status": "failed",
        "stage": "tts",
        "updated_at": now_iso(),
        "error": {
            "stage": "tts",
            "message": error_msg,
            "dlq_topic": config.TOPIC_DLQ,
        },
    }
    redis_client.set(f"job:{job_id}", json.dumps(state), ex=config.JOB_STATE_TTL_SECONDS)
