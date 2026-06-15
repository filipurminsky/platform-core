"""Redis job-state store (§5).

State is a single JSON document per job under `job:<job_id>`. `set_job_state`
merges an update into it *atomically* via a cjson Lua script, so two pods racing
on the same job during a rebalance/redelivery can't clobber each other's
accumulated `keys` (lost update).
"""

import json
from datetime import UTC, datetime

import redis as redis_client

from app import config

# Atomic read-merge-write. cjson runs inside Redis's single-threaded Lua VM, so
# GET → merge → SET is indivisible. `keys` is shallow-merged so stages accumulate
# (audio → transcript → summary → speech); other fields overwrite. updated_at is
# stamped server-side (ARGV[3]).
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
    return redis_client.from_url(
        config.REDIS_URL, password=config.REDIS_PASSWORD or None, decode_responses=True
    )


def set_job_state(r, job_id: str, update: dict) -> None:
    """Atomically merge `update` into the job:<id> document and refresh the TTL.

    The `keys` sub-dict is merged shallowly so downstream stages accumulate
    rather than overwrite.
    """
    r.eval(
        _MERGE_LUA,
        1,
        f"job:{job_id}",
        json.dumps(update),
        str(config.JOB_STATE_TTL_SECONDS),
        datetime.now(UTC).isoformat(),
    )
