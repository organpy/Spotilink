from __future__ import annotations

import asyncio
import contextlib

import pytest

from spotify_resolver.concurrency import (
    AsyncRateLimiter,
    KeyedLocks,
    SlidingWindowLimiter,
)


@pytest.mark.asyncio
async def test_keyed_locks_serialize_same_key() -> None:
    locks = KeyedLocks()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with locks.lock("same"):
            order.append(f"{name}:start")
            await asyncio.sleep(0.01)
            order.append(f"{name}:end")

    await asyncio.gather(worker("a"), worker("b"))
    assert order in (
        ["a:start", "a:end", "b:start", "b:end"],
        ["b:start", "b:end", "a:start", "a:end"],
    )


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_poison_lock() -> None:
    locks = KeyedLocks()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with locks.lock("key"):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with locks.lock("key"):
            pass

    holder_task = asyncio.create_task(holder())
    await entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiter_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter_task
    release.set()
    await holder_task

    async def acquire_again() -> None:
        async with locks.lock("key"):
            return

    await asyncio.wait_for(acquire_again(), timeout=1.0)
    assert locks._entries == {}


@pytest.mark.asyncio
async def test_rate_limiter_rejects_when_wait_exceeds_timeout() -> None:
    limiter = AsyncRateLimiter(1, burst=1)
    assert await limiter.acquire(timeout=0.0) is True
    assert await limiter.acquire(timeout=0.0) is False


@pytest.mark.asyncio
async def test_rate_limiter_can_be_disabled() -> None:
    limiter = AsyncRateLimiter(0, burst=1)
    assert limiter.enabled is False
    assert await limiter.acquire(timeout=0.0) is True


def test_sliding_window_limiter() -> None:
    clock = [1_000.0]
    limiter = SlidingWindowLimiter(2, clock=lambda: clock[0])
    assert limiter.check("client") is None
    assert limiter.check("client") is None
    wait = limiter.check("client")
    assert wait is not None and wait == pytest.approx(60.0)
    clock[0] += 60.0
    assert limiter.check("client") is None


def test_sliding_window_disabled() -> None:
    limiter = SlidingWindowLimiter(0)
    assert limiter.enabled is False
    assert limiter.check("client") is None
