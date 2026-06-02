"""Per-client-IP sliding-window rate limiter (good enough for demo scale)."""

import asyncio
import collections
import time


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter (1-minute window)."""

    WINDOW = 60  # seconds

    def __init__(self, limit: int):
        self._limit = limit
        self._windows: dict[str, collections.deque] = {}
        self._lock = asyncio.Lock()

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
