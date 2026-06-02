"""Redis job-state store (§5).

State is a single JSON document per job under `job:<job_id>`; `set_job_state`
merges an update into the current document and re-SETs it with the TTL.
"""

import json
from datetime import UTC, datetime

import redis as redis_client

from app import config


def make_redis_client():
    return redis_client.from_url(config.REDIS_URL, decode_responses=True)


def set_job_state(r, job_id: str, update: dict) -> None:
    """Merge `update` into the current job state JSON and re-SET with TTL."""
    key = f"job:{job_id}"
    raw = r.get(key)
    state = json.loads(raw) if raw else {}
    state.update(update)
    state["updated_at"] = datetime.now(UTC).isoformat()
    r.set(key, json.dumps(state), ex=config.JOB_STATE_TTL_SECONDS)
