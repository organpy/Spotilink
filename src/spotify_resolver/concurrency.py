from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    waiters: int = 0


class KeyedLocks:
    """One asyncio lock per key, cleaned up after the last waiter leaves."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._entries: dict[str, _LockEntry] = {}

    @asynccontextmanager
    async def lock(self, key: str) -> AsyncIterator[None]:
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(asyncio.Lock())
                self._entries[key] = entry
            entry.waiters += 1
            lock = entry.lock

        try:
            await lock.acquire()
            try:
                yield
            finally:
                lock.release()
        finally:
            async with self._guard:
                entry = self._entries.get(key)
                if entry is not None:
                    entry.waiters -= 1
                    if entry.waiters <= 0:
                        self._entries.pop(key, None)


class AsyncRateLimiter:
    """Small token bucket used to protect outbound provider APIs.

    A rate of zero disables limiting. The bucket starts full so a resolution
    can use its query variants without an artificial initial delay.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._rate = per_minute / 60.0 if per_minute > 0 else 0.0
        self._capacity = max(1, burst)
        self._tokens = float(self._capacity)
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._rate > 0

    async def acquire(self, timeout: float = 2.0) -> bool:
        if not self.enabled:
            return True
        async with self._lock:
            now = self._clock()
            self._refill(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            wait = (1.0 - self._tokens) / self._rate
            if wait > timeout:
                return False
            await self._sleep(wait)
            now = self._clock()
            self._refill(now)
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)


class SlidingWindowLimiter:
    """Per-client-key sliding window limiter for the public endpoint."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = per_minute
        self._window = 60.0
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def check(self, key: str) -> float | None:
        """Return None when allowed, otherwise the number of seconds to wait."""
        if not self.enabled:
            return None
        now = self._clock()
        hits = self._hits[key]
        cutoff = now - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) < self._limit:
            hits.append(now)
            self._cleanup(now)
            return None
        return max(0.0, hits[0] + self._window - now)

    def _cleanup(self, now: float) -> None:
        if len(self._hits) <= 10_000:
            return
        cutoff = now - self._window
        for key in list(self._hits):
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                self._hits.pop(key, None)
