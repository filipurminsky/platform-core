"""Per-client-IP sliding-window rate limiting.

Two implementations with the same async interface:

- ``SlidingWindowRateLimiter`` — in-process, per-pod. Correct only for a single
  replica (dev). With N replicas the effective per-IP limit is ~N× the configured
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
            expired_ips = []
            for ip, dq in self._windows.items():
                while dq and dq[0] < now - self.WINDOW:
                    dq.popleft()
                if not dq:
                    expired_ips.append(ip)
            for ip in expired_ips:
                del self._windows[ip]

    async def is_allowed(self, client_ip: str) -> tuple[bool, int]:
        """Returns (allowed, remaining)."""
        now = time.time()
        async with self._lock:
            dq = self._windows.setdefault(client_ip, collections.deque())
            # Drop timestamps outside the window
            while dq and dq[0] < now - self.WINDOW:
                dq.popleft()
            if len(dq) >= self._limit:
                return False, 0
            dq.append(now)
            return True, self._limit - len(dq)

    async def reset_at(self, client_ip: str) -> int:
        """Epoch second when the oldest request in the window expires."""
        async with self._lock:
            dq = self._windows.get(client_ip)
            if dq:
                return int(dq[0] + self.WINDOW)
            return int(time.time() + self.WINDOW)


# Atomic sliding window: prune expired entries, count, and conditionally admit —
# all in one round trip so two replicas hitting the same IP can't both read a
# stale count and over-admit. Returns {allowed, remaining}.
_SLIDING_WINDOW_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, now - window)
local count = redis.call('ZCARD', KEYS[1])
if count >= limit then
  return {0, 0}
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('EXPIRE', KEYS[1], math.ceil(window))
return {1, limit - count - 1}
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

    def _key(self, client_ip: str) -> str:
        return f"{self.KEY_PREFIX}{client_ip}"

    async def is_allowed(self, client_ip: str) -> tuple[bool, int]:
        now = time.time()
        # Unique member so two requests in the same millisecond don't collide on
        # an identical score (ZADD would otherwise overwrite, undercounting).
        member = f"{now:.6f}:{secrets.token_hex(4)}"
        try:
            allowed, remaining = await self._redis.eval(
                _SLIDING_WINDOW_LUA,
                1,
                self._key(client_ip),
                str(now),
                str(self.WINDOW),
                str(self._limit),
                member,
            )
            return bool(allowed), int(remaining)
        except Exception as exc:
            # Fail open: don't reject traffic because the limiter store is down.
            RATE_LIMITER_ERRORS.inc()
            log.warning("rate_limiter_unavailable", error=str(exc), client_ip=client_ip)
            return True, self._limit

    async def reset_at(self, client_ip: str) -> int:
        try:
            oldest = await self._redis.zrange(self._key(client_ip), 0, 0, withscores=True)
            if oldest:
                _member, score = oldest[0]
                return int(float(score) + self.WINDOW)
        except Exception as exc:
            RATE_LIMITER_ERRORS.inc()
            log.warning("rate_limiter_unavailable", error=str(exc), client_ip=client_ip)
        return int(time.time() + self.WINDOW)
