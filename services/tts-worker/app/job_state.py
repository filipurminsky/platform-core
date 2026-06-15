"""Redis job-state writes for the terminal (tts) stage.

Each helper merges its changes into the job:<id> JSON document *atomically* via a
cjson Lua script — preserving keys accumulated by earlier stages (audio,
transcript, summary) even if two pods race on the same job during a rebalance.
The terminal stage sets status synthesizing → done (or failed on DLQ).

Redis errors are logged and swallowed (never raise) — job-state writes are
best-effort; a transient Redis blip should not dead-letter a job whose actual
processing succeeded.  Aligns with the stt/llm-worker policy.
"""

import json
from datetime import UTC, datetime

import redis

from app import config
from app.observability import log

# Atomic read-merge-write. cjson runs inside Redis's single-threaded Lua VM, so
# GET → merge → SET is indivisible. `keys` is shallow-merged; other fields
# overwrite. updated_at is stamped server-side (ARGV[3]).
_MERGE_LUA = """
local raw = redis.call('GET', KEYS[1])
local cur = {}
if raw then cur = cjson.decode(raw) end
local update = cjson.decode(ARGV[1])
if type(update['keys']) == 'table' then
  local merged = {}
  if type(cur['keys']) == 'table' then
    for k, v in pairs(cur['keys']) do merged[k] = v end
  end
  for k, v in pairs(update['keys']) do merged[k] = v end
  cur['keys'] = merged
  update['keys'] = nil
end
for k, v in pairs(update) do cur[k] = v end
cur['updated_at'] = ARGV[3]
redis.call('SET', KEYS[1], cjson.encode(cur), 'EX', tonumber(ARGV[2]))
return 1
"""


def make_redis_client():
    return redis.from_url(
        config.REDIS_URL, password=config.REDIS_PASSWORD or None, decode_responses=True
    )


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _merge_state(redis_client, job_id: str, update: dict) -> None:
    try:
        redis_client.eval(
            _MERGE_LUA,
            1,
            f"job:{job_id}",
            json.dumps(update),
            str(config.JOB_STATE_TTL_SECONDS),
            now_iso(),
        )
    except Exception as exc:
        log.warning("redis_set_failed", job_id=job_id, error=str(exc))


def set_synthesizing(redis_client, job_id: str, summary_key: str) -> None:
    if not job_id:
        return
    _merge_state(
        redis_client,
        job_id,
        {"status": "synthesizing", "stage": "tts", "keys": {"summary": summary_key}},
    )


def set_done(redis_client, job_id: str, summary_key: str, speech_key: str) -> None:
    if not job_id:
        return
    _merge_state(
        redis_client,
        job_id,
        {
            "status": "done",
            "stage": "tts",
            "keys": {"summary": summary_key, "speech": speech_key},
        },
    )


def set_failed(redis_client, job_id: str, error_msg: str) -> None:
    if not job_id:
        return
    _merge_state(
        redis_client,
        job_id,
        {
            "status": "failed",
            "stage": "tts",
            "error": {
                "stage": "tts",
                "message": error_msg,
                "dlq_topic": config.TOPIC_DLQ,
            },
        },
    )
