"""Per-client sliding-window rate limiting.

The identity is an opaque string chosen by the caller: a hashed API-key id when
auth is on, otherwise the (proxy-aware) client IP.

Two implementations with the same async interface:

- ``SlidingWindowRateLimiter`` — in-process, per-pod. Correct only for a single
  replica (dev). With N replicas the effective per-client limit is ~N× the configured
  value because each pod keeps its own window.
- ``RedisSlidingWindowRateLimiter`` — shared across all replicas via a Redis
  sorted set, mutated atomically by a Lua script so concurrent pods can't race.
  This is what makes the limit hold at its configured value in prod (2 replicas).
  Fails open (allow + log) if Redis is unreachable, so a Redis blip degrades
  rate limiting rather than taking the gateway down.
"""

import asyncio
import collections
import secrets
import time

from app.metrics import RATE_LIMITER_ERRORS
from app.observability import log


class SlidingWindowRateLimiter:
    """Thread-safe in-process sliding window rate limiter (1-minute window)."""

    WINDOW = 60  # seconds
    CLEANUP_INTERVAL = 300  # seconds (5 minutes)

    def __init__(self, limit: int):
        self._limit = limit
        self._windows: dict[str, collections.deque] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    def start_cleanup(self):
        """Start the background cleanup task."""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        """Periodically remove expired windows to bound memory usage."""
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            await self.cleanup()

    async def cleanup(self):
        """Remove windows where all timestamps are outside the window."""
        now = time.time()
        async with self._lock:
            expired = []
            for identity, dq in self._windows.items():
                while dq and dq[0] < now - self.WINDOW:
                    dq.popleft()
                if not dq:
                    expired.append(identity)
            for identity in expired:
                del self._windows[identity]

    async def is_allowed(self, identity: str) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, reset_at).

        ``reset_at`` is the epoch second at which the oldest request in the window
        expires (i.e. when a slot frees up), computed under the same lock as the
        admission decision so the three values are mutually consistent.
        """
        now = time.time()
        async with self._lock:
            dq = self._windows.setdefault(identity, collections.deque())
            # Drop timestamps outside the window
            while dq and dq[0] < now - self.WINDOW:
                dq.popleft()
            if len(dq) >= self._limit:
                return False, 0, int(dq[0] + self.WINDOW)
            dq.append(now)
            return True, self._limit - len(dq), int(dq[0] + self.WINDOW)


# Atomic sliding window: prune expired entries, count, and conditionally admit —
# all in one round trip so two replicas hitting the same identity can't both read
# a stale count and over-admit. Returns {allowed, remaining, reset_at}, where
# reset_at is the epoch second the oldest in-window entry expires (Lua numbers are
# truncated to integers in the reply, so it is floored explicitly).
_SLIDING_WINDOW_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local count = redis.call('ZCARD', KEYS[1])
local allowed = 0
local remaining = 0
if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  redis.call('EXPIRE', KEYS[1], math.ceil(window))
  allowed = 1
  remaining = limit - count - 1
end
local reset_at = now + window
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
if oldest[2] then
  reset_at = tonumber(oldest[2]) + window
end
return {allowed, remaining, math.floor(reset_at)}
"""


class RedisSlidingWindowRateLimiter:
    """Sliding window shared across replicas, backed by a Redis sorted set."""

    WINDOW = 60  # seconds
    KEY_PREFIX = "ratelimit:gateway:"

    def __init__(self, limit: int, redis_client):
        self._limit = limit
        self._redis = redis_client

    def start_cleanup(self):
        """No-op: Redis EXPIRE reclaims idle windows; nothing to sweep locally."""

    def _key(self, identity: str) -> str:
        return f"{self.KEY_PREFIX}{identity}"

    async def is_allowed(self, identity: str) -> tuple[bool, int, int]:
        """Returns (allowed, remaining, reset_at) from one atomic Redis round trip."""
        now = time.time()
        # Unique member so two requests in the same millisecond don't collide on
        # an identical score (ZADD would otherwise overwrite, undercounting).
        member = f"{now:.6f}:{secrets.token_hex(4)}"
        try:
            allowed, remaining, reset_at = await self._redis.eval(
                _SLIDING_WINDOW_LUA,
                1,
                self._key(identity),
                str(now),
                str(self.WINDOW),
                str(self._limit),
                member,
            )
            return bool(allowed), int(remaining), int(reset_at)
        except Exception as exc:
            # Fail open: don't reject traffic because the limiter store is down.
            RATE_LIMITER_ERRORS.inc()
            log.warning("rate_limiter_unavailable", error=str(exc), identity=identity)
            return True, self._limit, int(now + self.WINDOW)
