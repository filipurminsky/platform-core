"""Per-client-IP sliding-window rate limiter (good enough for demo scale)."""

import asyncio
import collections
import time


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter (1-minute window)."""

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
