from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import make_http_client

from spotify_resolver.concurrency import AsyncRateLimiter
from spotify_resolver.errors import (
    ProviderAuthError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUpstreamError,
)
from spotify_resolver.providers.youtube import YouTubeProvider, parse_iso8601_duration

VIDEO_ID = "dQw4w9WgXcQ"


def _search_response(*video_ids: str) -> dict[str, Any]:
    return {
        "items": [{"id": {"videoId": video_id}} for video_id in video_ids],
    }


def _video_item(
    video_id: str = VIDEO_ID,
    *,
    title: str = "Never Gonna Give You Up",
    channel: str = "Rick Astley - Topic",
    duration: str = "PT3M33S",
    privacy: str = "public",
    upload_status: str = "processed",
    live: str = "none",
) -> dict[str, Any]:
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "channelId": "UCtest",
            "liveBroadcastContent": live,
        },
        "contentDetails": {"duration": duration},
        "status": {
            "privacyStatus": privacy,
            "uploadStatus": upload_status,
            "embeddable": True,
        },
    }


def _transport(
    *,
    search: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    calls: list[str] | None = None,
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200, json=search or _search_response(VIDEO_ID), request=request
            )
        if request.url.path.endswith("/videos"):
            return httpx.Response(
                200, json=details or {"items": [_video_item()]}, request=request
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_youtube_search_maps_candidate(make_settings) -> None:
    settings = make_settings(youtube_api_key="test-key")
    calls: list[str] = []
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        requests.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json=_search_response(VIDEO_ID), request=request)
        return httpx.Response(200, json={"items": [_video_item()]}, request=request)

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        provider = YouTubeProvider(http, settings)
        candidates = await provider.search("Rick Astley Never Gonna Give You Up", 5)
    finally:
        await http.aclose()

    assert len(candidates) == 1
    item = candidates[0]
    assert item.platform == "youtube"
    assert item.url == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert item.title == "Never Gonna Give You Up"
    assert item.artist == "Rick Astley - Topic"
    assert item.duration_ms == 213_000
    assert item.is_topic is True
    assert item.channel_id == "UCtest"
    assert calls == ["/youtube/v3/search", "/youtube/v3/videos"]
    assert requests[0].url.params["key"] == "test-key"
    assert requests[0].url.params["type"] == "video"
    assert requests[1].url.params["id"] == VIDEO_ID


@pytest.mark.asyncio
async def test_youtube_filters_private_and_live_videos(make_settings) -> None:
    settings = make_settings(youtube_api_key="test-key")
    details = {
        "items": [
            _video_item("aaaaaaaaaaa"),
            _video_item("bbbbbbbbbbb", privacy="private"),
            _video_item("ccccccccccc", live="live"),
            _video_item("ddddddddddd", upload_status="failed"),
        ]
    }
    transport = _transport(
        search=_search_response(
            "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"
        ),
        details=details,
    )
    _raw, http = make_http_client(settings, transport)
    try:
        candidates = await YouTubeProvider(http, settings).search("query", 10)
    finally:
        await http.aclose()
    assert [item.url.rsplit("=", 1)[1] for item in candidates] == ["aaaaaaaaaaa"]


@pytest.mark.asyncio
async def test_youtube_missing_key_is_not_configured(make_settings) -> None:
    settings = make_settings()
    provider = YouTubeProvider(object(), settings)  # type: ignore[arg-type]
    assert provider.configured is False
    with pytest.raises(ProviderNotConfigured):
        await provider.search("query", 5)


@pytest.mark.asyncio
async def test_youtube_quota_error_is_rate_limited(make_settings) -> None:
    settings = make_settings(youtube_api_key="test-key", http_max_retries=0)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"errors": [{"reason": "quotaExceeded"}]}},
            request=request,
        )

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderRateLimited):
            await YouTubeProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_youtube_invalid_key_is_auth_error(make_settings) -> None:
    settings = make_settings(youtube_api_key="test-key", http_max_retries=0)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"errors": [{"reason": "keyInvalid"}]}},
            request=request,
        )

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderAuthError):
            await YouTubeProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_youtube_retries_transient_500(make_settings) -> None:
    settings = make_settings(youtube_api_key="test-key", http_max_retries=1)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, request=request)
        return httpx.Response(200, json={"items": []}, request=request)

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        candidates = await YouTubeProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()
    assert candidates == []
    assert calls == 2


@pytest.mark.asyncio
async def test_youtube_timeout(make_settings) -> None:
    settings = make_settings(youtube_api_key="test-key", http_max_retries=0)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderTimeout):
            await YouTubeProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_youtube_malformed_json(make_settings) -> None:
    settings = make_settings(youtube_api_key="test-key", http_max_retries=0)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="{broken", request=request)

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderUpstreamError):
            await YouTubeProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_youtube_local_rate_limit(make_settings) -> None:
    settings = make_settings(
        youtube_api_key="test-key",
        provider_rate_limit_wait_seconds=0.0,
    )
    limiter = AsyncRateLimiter(1, burst=1)
    transport = _transport(search={"items": []})
    _raw, http = make_http_client(settings, transport)
    try:
        provider = YouTubeProvider(http, settings, limiter=limiter)
        assert await provider.search("first", 5) == []
        with pytest.raises(ProviderRateLimited):
            await provider.search("second", 5)
    finally:
        await http.aclose()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("PT3M33S", 213_000),
        ("PT3M33.573S", 213_573),
        ("PT1H2M3S", 3_723_000),
        ("P1DT1S", 86_401_000),
        ("PT0S", None),
        ("P0D", None),
        ("", None),
        (None, None),
        ("3:33", None),
    ],
)
def test_parse_iso8601_duration(value, expected) -> None:
    assert parse_iso8601_duration(value) == expected
