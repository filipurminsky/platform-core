"""Redis job-state store (§5).

State is a single JSON document per job under `job:<job_id>`. The mark_* helpers
merge into that document *atomically* via a cjson Lua script (so concurrent
writers during a rebalance can't clobber accumulated `keys`), and never raise —
Redis hiccups are logged, not fatal to processing.
"""

import json
from datetime import UTC, datetime

import redis as redis_lib

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
    return redis_lib.from_url(
        config.REDIS_URL, password=config.REDIS_PASSWORD or None, decode_responses=True
    )


def _merge_state(r, job_id: str, update: dict) -> None:
    try:
        r.eval(
            _MERGE_LUA,
            1,
            f"job:{job_id}",
            json.dumps(update),
            str(config.JOB_STATE_TTL),
            datetime.now(UTC).isoformat(),
        )
    except Exception as exc:
        log.warning("redis_set_failed", job_id=job_id, error=str(exc))


def mark_summarizing(r, job_id: str) -> None:
    _merge_state(r, job_id, {"status": "summarizing", "stage": "llm"})


def mark_summarized(r, job_id: str, summary_key: str) -> None:
    # Status stays "summarizing" — tts-worker transitions to synthesizing/done.
    # The pipeline contract has no "summarized" intermediate state.
    _merge_state(
        r,
        job_id,
        {"status": "summarizing", "stage": "llm", "keys": {"summary": summary_key}},
    )


def mark_failed(r, job_id: str, error_msg: str) -> None:
    _merge_state(
        r,
        job_id,
        {
            "status": "failed",
            "stage": "llm",
            "error": {
                "stage": "llm",
                "message": error_msg,
                "dlq_topic": config.TOPIC_DLQ,
            },
        },
    )
