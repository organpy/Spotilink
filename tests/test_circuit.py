from __future__ import annotations

from conftest import MockClock

from spotify_resolver.circuit import CircuitBreaker


def test_breaker_opens_and_probes_after_cooldown() -> None:
    clock = MockClock(0.0)
    breaker = CircuitBreaker("p", 300, clock=clock)
    assert breaker.allow()
    breaker.record_failure("rate_limited")
    assert breaker.state == "open" and not breaker.allow()
    clock.advance(299)
    assert not breaker.allow()
    clock.advance(2)
    assert breaker.state == "half_open"
    assert breaker.allow()  # the single probe
    assert not breaker.allow()  # no concurrent probes
    breaker.record_success()
    assert breaker.state == "closed" and breaker.allow()


def test_failed_probe_reopens() -> None:
    clock = MockClock(0.0)
    breaker = CircuitBreaker("p", 10, clock=clock)
    breaker.record_failure("rate_limited")
    clock.advance(11)
    assert breaker.allow()
    breaker.record_failure("rate_limited")
    assert not breaker.allow()
    assert breaker.snapshot()["trips"] == 2


def test_retry_after_extends_cooldown() -> None:
    clock = MockClock(0.0)
    breaker = CircuitBreaker("p", 10, clock=clock)
    breaker.record_failure("rate_limited", retry_after_seconds=120)
    clock.advance(60)
    assert not breaker.allow()
    clock.advance(61)
    assert breaker.allow()


def test_release_allows_new_probe() -> None:
    clock = MockClock(0.0)
    breaker = CircuitBreaker("p", 1, clock=clock)
    breaker.record_failure("x")
    clock.advance(2)
    assert breaker.allow()
    breaker.release()
    assert breaker.allow()
