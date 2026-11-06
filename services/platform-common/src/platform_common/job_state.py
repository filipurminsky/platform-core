"""Redis job-state store (contract §5).

State is one JSON document per job under ``job:<job_id>``. ``merge_job_state``
merges an update into it *atomically* via a cjson Lua script, so two pods racing
on the same job during a rebalance/redelivery can't clobber each other's
accumulated ``keys`` (lost update). Writes are best-effort: errors are logged
and swallowed — a Redis blip must not dead-letter a job whose real work succeeded.
"""

from __future__ import annotations

import json

import redis

from platform_common.events import now_iso
from platform_common.observability import log

# GET → merge → SET inside Redis's single-threaded Lua VM is indivisible. `keys`
# is shallow-merged so stages accumulate (audio → transcript → summary →
# speech); other fields overwrite. updated_at is stamped from ARGV[3].
MERGE_LUA = """
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


def make_redis_client(url: str, password: str = "", *, socket_timeout: float = 5.0):
    return redis.from_url(
        url,
        password=password or None,
        decode_responses=True,
        socket_timeout=socket_timeout,
        socket_connect_timeout=socket_timeout,
        health_check_interval=30,
    )


def merge_job_state(r, job_id: str | None, update: dict, ttl_seconds: int) -> bool:
    """Atomically merge ``update`` into ``job:<job_id>``; refresh TTL. Never raises."""
    if not job_id:
        return False
    try:
        r.eval(MERGE_LUA, 1, f"job:{job_id}", json.dumps(update), str(ttl_seconds), now_iso())
        return True
    except Exception as exc:
        log.warning("redis_state_update_failed", job_id=job_id, error=str(exc))
        return False
