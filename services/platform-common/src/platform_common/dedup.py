"""Atomic per-stage job dedup in Redis.

``claim`` does ``SET dedup:<stage>:<job_id> <token> NX EX <lease>`` *before*
processing, so two pods can never both start the same job (no check-then-act
window). On success or dead-letter the key is overwritten with ``done`` for the
job-state TTL; if the pod crashes the lease simply expires and a redelivered
message can be claimed again. Redis being unreachable fails *open* (process
anyway): at-least-once with idempotent writes beats stalling the pipeline.
"""

from __future__ import annotations

import enum
import socket
import uuid

from platform_common.observability import log

DONE = "done"
_LEGACY_DONE = "1"  # value written by pre-lease versions of the workers

# Compare-and-delete: only release a claim we still own.
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class Claim(enum.Enum):
    CLAIMED = "claimed"
    ALREADY_DONE = "already_done"
    IN_PROGRESS = "in_progress"  # another consumer holds a live lease
    UNAVAILABLE = "unavailable"  # Redis error → fail open


class Deduper:
    def __init__(self, r, stage: str, *, lease_seconds: int, done_ttl_seconds: int):
        self.r = r
        self.stage = stage
        self.lease_seconds = lease_seconds
        self.done_ttl_seconds = done_ttl_seconds
        self._tokens: dict[str, str] = {}

    def key(self, job_id: str) -> str:
        return f"dedup:{self.stage}:{job_id}"

    def claim(self, job_id: str) -> Claim:
        token = f"processing:{socket.gethostname()}:{uuid.uuid4().hex}"
        key = self.key(job_id)
        try:
            # Two tries: the holder's lease may expire between SET NX and GET.
            for _ in range(2):
                if self.r.set(key, token, nx=True, ex=self.lease_seconds):
                    self._tokens[job_id] = token
                    return Claim.CLAIMED
                current = self.r.get(key)
                if current in (DONE, _LEGACY_DONE):
                    return Claim.ALREADY_DONE
                if current is not None:
                    return Claim.IN_PROGRESS
        except Exception as exc:
            log.warning("dedup_claim_failed", job_id=job_id, error=str(exc))
            return Claim.UNAVAILABLE
        return Claim.IN_PROGRESS

    def mark_done(self, job_id: str) -> None:
        self._tokens.pop(job_id, None)
        try:
            self.r.set(self.key(job_id), DONE, ex=self.done_ttl_seconds)
        except Exception as exc:
            log.warning("dedup_set_failed", job_id=job_id, error=str(exc))

    def release(self, job_id: str) -> None:
        token = self._tokens.pop(job_id, None)
        if token is None:
            return
        try:
            self.r.eval(_RELEASE_LUA, 1, self.key(job_id), token)
        except Exception as exc:
            log.warning("dedup_release_failed", job_id=job_id, error=str(exc))
