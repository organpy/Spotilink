from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import MockClock, make_http_client

from spotify_resolver.errors import (
    ProviderAuthError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderUpstreamError,
)
from spotify_resolver.providers.soundcloud import (
    SOUNDCLOUD_API_HOST,
    SOUNDCLOUD_TOKEN_HOST,
    SoundCloudProvider,
    _canonical_soundcloud_url,
)

TOKEN_URL_HOST = SOUNDCLOUD_TOKEN_HOST
API_URL_HOST = SOUNDCLOUD_API_HOST


def _track(
    *,
    title: str = "Never Gonna Give You Up",
    duration: int = 213_000,
    permalink: str = "https://soundcloud.com/rick-astley/never-gonna-give-you-up",
    kind: str = "track",
    metadata_artist: str | None = "Rick Astley",
    uploader: str = "Rick Astley",
    verified: bool = True,
    access: str = "playable",
    sharing: str = "public",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "duration": duration,
        "permalink_url": permalink,
        "access": access,
        "sharing": sharing,
        "user": {
            "id": 123,
            "username": uploader,
            "full_name": uploader,
            "verified": verified,
        },
    }
    if metadata_artist is not None:
        item["metadata_artist"] = metadata_artist
    return item


def _transport(
    *,
    collection: list[dict[str, Any]] | None = None,
    token_calls: list[int] | None = None,
    search_calls: list[int] | None = None,
    token_status: int = 200,
    search_statuses: list[int] | None = None,
    token_expires_in: int = 3600,
) -> httpx.MockTransport:
    search_statuses = list(search_statuses or [])

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_URL_HOST:
            if token_calls is not None:
                token_calls[0] += 1
            if token_status != 200:
                return httpx.Response(token_status, request=request)
            return httpx.Response(
                200,
                json={
                    "access_token": f"token-{(token_calls[0] if token_calls else 1)}",
                    "expires_in": token_expires_in,
                    "token_type": "bearer",
                },
                request=request,
            )
        if request.url.host == API_URL_HOST and request.url.path == "/tracks":
            if search_calls is not None:
                search_calls[0] += 1
            if search_statuses:
                status = search_statuses.pop(0)
                if status != 200:
                    return httpx.Response(status, request=request)
            return httpx.Response(
                200,
                json={"collection": collection or []},
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_soundcloud_search_maps_tracks_and_filters(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
    )
    token_calls = [0]
    search_calls = [0]
    collection = [
        _track(
            permalink="http://www.soundcloud.com/rick-astley/never-gonna-give-you-up?si=abc",
        ),
        _track(
            kind="playlist", permalink="https://soundcloud.com/rick-astley/sets/hits"
        ),
        _track(access="blocked"),
        _track(access="preview"),
        _track(permalink="https://evil.example/rick-astley/song"),
        _track(permalink="https://soundcloud.com/rick-astley/song/extra"),
        _track(duration=0),
        _track(
            title="Second Song",
            metadata_artist=None,
            uploader="Uploader Name",
            verified=False,
        ),
    ]
    missing_kind = _track(title="Missing kind")
    missing_kind.pop("kind")
    collection.append(missing_kind)
    transport = _transport(
        collection=collection,
        token_calls=token_calls,
        search_calls=search_calls,
    )
    _raw, http = make_http_client(settings, transport)
    try:
        candidates = await SoundCloudProvider(http, settings).search("Rick Astley", 5)
    finally:
        await http.aclose()

    assert token_calls[0] == 1
    assert search_calls[0] == 1
    assert len(candidates) == 2
    first, second = candidates
    assert first.platform == "soundcloud"
    assert first.url == "https://soundcloud.com/rick-astley/never-gonna-give-you-up"
    assert first.title == "Never Gonna Give You Up"
    assert first.artist == "Rick Astley"
    assert first.duration_ms == 213_000
    assert first.official_hint is True
    assert second.artist == "Uploader Name"
    assert second.official_hint is False


@pytest.mark.asyncio
async def test_soundcloud_search_params_and_auth_header(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
    )
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == TOKEN_URL_HOST:
            assert request.method == "POST"
            assert b"grant_type=client_credentials" in request.content
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
                request=request,
            )
        assert request.headers["authorization"] == "OAuth token"
        assert request.url.params["q"] == "Rick Astley"
        assert request.url.params["access"] == "playable"
        assert request.url.params["linked_partitioning"] == "true"
        return httpx.Response(200, json={"collection": []}, request=request)

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        await SoundCloudProvider(http, settings).search("Rick Astley", 5)
    finally:
        await http.aclose()
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_soundcloud_token_is_cached(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
    )
    token_calls = [0]
    search_calls = [0]
    _raw, http = make_http_client(
        settings,
        _transport(
            collection=[_track()],
            token_calls=token_calls,
            search_calls=search_calls,
        ),
    )
    try:
        provider = SoundCloudProvider(http, settings)
        await provider.search("one", 5)
        await provider.search("two", 5)
    finally:
        await http.aclose()
    assert token_calls[0] == 1
    assert search_calls[0] == 2


@pytest.mark.asyncio
async def test_soundcloud_refreshes_token_after_401(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
        http_max_retries=0,
    )
    token_calls = [0]
    search_calls = [0]
    transport = _transport(
        collection=[_track()],
        token_calls=token_calls,
        search_calls=search_calls,
        search_statuses=[401],
    )
    _raw, http = make_http_client(settings, transport)
    try:
        candidates = await SoundCloudProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()
    assert len(candidates) == 1
    assert token_calls[0] == 2
    assert search_calls[0] == 2


@pytest.mark.asyncio
async def test_soundcloud_maps_rate_limit(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
        http_max_retries=0,
    )
    _raw, http = make_http_client(
        settings,
        _transport(search_statuses=[429]),
    )
    try:
        with pytest.raises(ProviderRateLimited):
            await SoundCloudProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_soundcloud_maps_forbidden(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
        http_max_retries=0,
    )
    _raw, http = make_http_client(
        settings,
        _transport(search_statuses=[403]),
    )
    try:
        with pytest.raises(ProviderAuthError):
            await SoundCloudProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_soundcloud_malformed_search_json(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
        http_max_retries=0,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_URL_HOST:
            return httpx.Response(
                200,
                json={"access_token": "token", "expires_in": 3600},
                request=request,
            )
        return httpx.Response(200, text="{broken", request=request)

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderUpstreamError):
            await SoundCloudProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_soundcloud_malformed_token_response(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
        http_max_retries=0,
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, request=request)

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderAuthError):
            await SoundCloudProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_soundcloud_token_endpoint_auth_error(make_settings) -> None:
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
        http_max_retries=0,
    )
    _raw, http = make_http_client(settings, _transport(token_status=401))
    try:
        with pytest.raises(ProviderAuthError):
            await SoundCloudProvider(http, settings).search("query", 5)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_soundcloud_missing_credentials(make_settings) -> None:
    settings = make_settings()
    provider = SoundCloudProvider(object(), settings)  # type: ignore[arg-type]
    assert provider.configured is False
    with pytest.raises(ProviderNotConfigured):
        await provider.search("query", 5)


@pytest.mark.asyncio
async def test_soundcloud_token_expiry_refreshes(make_settings) -> None:
    clock = MockClock()
    settings = make_settings(
        soundcloud_client_id="client",
        soundcloud_client_secret="secret",
    )
    token_calls = [0]
    transport = _transport(
        collection=[_track()],
        token_calls=token_calls,
        token_expires_in=120,
    )
    _raw, http = make_http_client(settings, transport)
    try:
        provider = SoundCloudProvider(http, settings, clock=clock)
        await provider.search("one", 5)
        clock.advance(61)
        await provider.search("two", 5)
        clock.advance(30)
        await provider.search("three", 5)
    finally:
        await http.aclose()
    assert token_calls[0] == 2


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://soundcloud.com/artist/song", "https://soundcloud.com/artist/song"),
        (
            "http://www.soundcloud.com/artist/song?si=abc",
            "https://soundcloud.com/artist/song",
        ),
        ("https://m.soundcloud.com/artist/song/", "https://soundcloud.com/artist/song"),
        ("https://soundcloud.com/artist/sets/hits", None),
        ("https://soundcloud.com/artist/song/extra", None),
        ("ftp://soundcloud.com/artist/song", None),
        ("https://user@soundcloud.com/artist/song", None),
        ("https://soundcloud.com:8443/artist/song", None),
        ("https://soundcloud.com/search?q=x", None),
        ("https://soundcloud.com/artist", None),
        ("https://evil.example/artist/song", None),
        ("not a url", None),
        (None, None),
    ],
)
def test_canonical_soundcloud_url(value, expected) -> None:
    assert _canonical_soundcloud_url(value) == expected
