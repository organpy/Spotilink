from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from spotify_resolver.http import (
    HttpDecodeError,
    HttpRedirectError,
    HttpResponseTooLarge,
    HttpStatusError,
    HttpTimeoutError,
    LimitedHttpClient,
)


def _client(
    transport: httpx.AsyncBaseTransport,
    *,
    max_retries: int = 0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> LimitedHttpClient:
    raw = httpx.AsyncClient(transport=transport, timeout=None, follow_redirects=False)
    return LimitedHttpClient(
        raw,
        max_retries=max_retries,
        retry_backoff_seconds=0.0,
        retry_after_cap_seconds=0.0,
        sleep=sleep,
    )


@pytest.mark.asyncio
async def test_retries_429_and_honors_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, request=request)
        return httpx.Response(200, text="ok", request=request)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = _client(httpx.MockTransport(handler), max_retries=1, sleep=fake_sleep)
    try:
        text = await client.get_text(
            "https://example.com/resource",
            max_bytes=100,
            timeout=1,
            allowed_hosts={"example.com"},
        )
    finally:
        await client.aclose()
    assert text == "ok"
    assert calls == 2
    assert sleeps == [0.0]


@pytest.mark.asyncio
async def test_timeout_is_not_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(httpx.MockTransport(handler), max_retries=3)
    try:
        with pytest.raises(HttpTimeoutError):
            await client.get_text(
                "https://example.com/resource",
                max_bytes=100,
                timeout=1,
                allowed_hosts={"example.com"},
            )
    finally:
        await client.aclose()
    assert calls == 1


@pytest.mark.asyncio
async def test_response_size_limit_is_stream_enforced() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 50, request=request)

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(HttpResponseTooLarge):
            await client.get_text(
                "https://example.com/resource",
                max_bytes=10,
                timeout=1,
                allowed_hosts={"example.com"},
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_follows_allowed_redirect() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"location": "https://example.com/final"},
                request=request,
            )
        return httpx.Response(200, text="final", request=request)

    client = _client(httpx.MockTransport(handler))
    try:
        text = await client.get_text(
            "https://example.com/start",
            max_bytes=100,
            timeout=1,
            allowed_hosts={"example.com"},
            max_redirects=1,
        )
    finally:
        await client.aclose()
    assert text == "final"


@pytest.mark.asyncio
async def test_rejects_redirect_to_disallowed_host() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://evil.example/final"},
            request=request,
        )

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(HttpRedirectError):
            await client.get_text(
                "https://example.com/start",
                max_bytes=100,
                timeout=1,
                allowed_hosts={"example.com"},
                max_redirects=1,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_too_many_redirects() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://example.com/final"},
            request=request,
        )

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(HttpRedirectError):
            await client.get_text(
                "https://example.com/start",
                max_bytes=100,
                timeout=1,
                allowed_hosts={"example.com"},
                max_redirects=0,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_malformed_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{broken", request=request)

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(HttpDecodeError):
            await client.get_json(
                "https://example.com/resource",
                max_bytes=100,
                timeout=1,
                allowed_hosts={"example.com"},
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_retries_500_then_raises_status() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="nope", request=request)

    client = _client(httpx.MockTransport(handler), max_retries=1)
    try:
        with pytest.raises(HttpStatusError) as error:
            await client.get_text(
                "https://example.com/resource",
                max_bytes=100,
                timeout=1,
                allowed_hosts={"example.com"},
            )
    finally:
        await client.aclose()
    assert error.value.status_code == 500
    assert error.value.retryable is True
    assert calls == 2


@pytest.mark.asyncio
async def test_rejects_non_https_initial_url() -> None:
    client = _client(httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        with pytest.raises(HttpRedirectError):
            await client.get_text(
                "http://example.com/resource",
                max_bytes=100,
                timeout=1,
                allowed_hosts={"example.com"},
            )
    finally:
        await client.aclose()
