"""Minimal per-process circuit breaker for the primary YouTube resolver.

closed     -> requests go to the provider.
open       -> the provider is skipped until the cooldown (or a longer
              Retry-After) elapses.
half_open  -> exactly one probe request is let through; success closes the
              circuit, another quota/rate-limit failure reopens it.

State lives in memory. Each worker process learns about quota exhaustion
independently, after at most one failed request.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        cooldown_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._state = "closed"
        self._open_until = 0.0
        self._probe_in_flight = False
        self._trips = 0
        self._last_reason: str | None = None

    @property
    def state(self) -> str:
        if self._state == "open" and self._clock() >= self._open_until:
            return "half_open"
        return self._state

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open":
            if self._probe_in_flight:
                return False
            self._state = "half_open"
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._state = "closed"
        self._probe_in_flight = False
        self._last_reason = None

    def record_failure(self, reason: str, retry_after_seconds: float | None = None) -> None:
        delay = self._cooldown
        if retry_after_seconds is not None and retry_after_seconds > delay:
            delay = retry_after_seconds
        self._state = "open"
        self._open_until = self._clock() + delay
        self._probe_in_flight = False
        self._trips += 1
        self._last_reason = reason

    def release(self) -> None:
        """End a probe that produced no verdict (e.g. a timeout)."""
        self._probe_in_flight = False

    def snapshot(self) -> dict[str, Any]:
        state = self.state
        remaining = max(0.0, self._open_until - self._clock()) if state == "open" else 0.0
        return {
            "state": state,
            "retry_in_seconds": round(remaining, 1),
            "trips": self._trips,
            "last_reason": self._last_reason,
        }
