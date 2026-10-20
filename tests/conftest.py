from __future__ import annotations

import asyncio
import base64
import html
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from spotify_resolver.config import Settings
from spotify_resolver.models import Candidate, SpotifyTrack
from spotify_resolver.providers.base import Provider

TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
TRACK_TITLE = "Never Gonna Give You Up"
TRACK_ARTISTS = ("Rick Astley",)
TRACK_ALBUM = "Whenever You Need Somebody"
TRACK_DURATION_MS = 213_573


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        base = Settings(
            database_path=tmp_path / "resolver.db",
            http_retry_backoff_seconds=0.0,
            http_retry_after_cap_seconds=0.0,
            provider_rate_limit_wait_seconds=0.0,
            youtube_rate_limit_per_minute=0,
            soundcloud_rate_limit_per_minute=0,
            api_rate_limit_per_minute=0,
        )
        return replace(base, **overrides)

    return factory


@pytest.fixture
def sample_track() -> SpotifyTrack:
    return SpotifyTrack(
        spotify_id=TRACK_ID,
        title=TRACK_TITLE,
        artists=TRACK_ARTISTS,
        album=TRACK_ALBUM,
        duration_ms=TRACK_DURATION_MS,
        duration_precision="ms",
    )


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


class MockClock:
    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeSpotify:
    def __init__(
        self,
        track: SpotifyTrack | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.track = track
        self.error = error
        self.delay = delay
        self.calls = 0

    async def get_track(self, track_id: str) -> SpotifyTrack:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.track is None:
            raise AssertionError("FakeSpotify has no track configured")
        return self.track


class FakeProvider(Provider):
    def __init__(
        self,
        name: str,
        candidates: list[Candidate] | None = None,
        *,
        configured: bool = True,
        errors: list[Exception | None] | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self.candidates = list(candidates or [])
        self._configured = configured
        self.errors = list(errors or [])
        self.error = error
        self.delay = delay
        self.calls: list[str] = []

    @property
    def configured(self) -> bool:
        return self._configured

    async def search(self, query: str, limit: int) -> list[Candidate]:
        self.calls.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        if self.error is not None:
            raise self.error
        return list(self.candidates)


def iso_duration(duration_ms: int) -> str:
    total_seconds = duration_ms / 1000.0
    minutes = int(total_seconds // 60)
    seconds = total_seconds - minutes * 60
    if seconds == int(seconds):
        second_text = str(int(seconds))
    else:
        second_text = f"{seconds:.3f}".rstrip("0").rstrip(".")
    return f"PT{minutes}M{second_text}S"


def build_spotify_page(
    *,
    track_id: str = TRACK_ID,
    title: str = TRACK_TITLE,
    artists: tuple[str, ...] = TRACK_ARTISTS,
    album: str = TRACK_ALBUM,
    duration_ms: int | None = TRACK_DURATION_MS,
    include_jsonld: bool = True,
    include_state: bool = True,
    include_meta: bool = True,
    include_duration_meta: bool = True,
    malformed_jsonld: bool = False,
    malformed_state: bool = False,
    state_track_id: str | None = None,
    state_title: str | None = None,
    state_artists: tuple[str, ...] | None = None,
    state_duration_ms: int | None | object = ...,
    shell: bool = False,
) -> str:
    if shell:
        return (
            "<!doctype html><html><head><title>Spotify</title></head>"
            '<body><div id="main">Spotify</div></body></html>'
        )

    parts = [
        "<!doctype html><html><head>",
        f"<title>{html.escape(title)} | Spotify</title>",
    ]
    if include_meta:
        parts.extend(
            [
                f'<meta property="og:title" content="{html.escape(title, quote=True)}">',
                (
                    '<meta property="og:description" '
                    f'content="{html.escape(f"{artists[0]} · {album} · Song · 1987", quote=True)}">'
                ),
                (
                    '<meta property="music:musician_description" '
                    f'content="{html.escape(artists[0], quote=True)}">'
                ),
            ]
        )
        if include_duration_meta and duration_ms is not None:
            parts.append(
                f'<meta property="music:duration" content="{round(duration_ms / 1000)}">'
            )
    if include_jsonld:
        if malformed_jsonld:
            raw_jsonld = "{not valid json"
        else:
            jsonld: dict[str, Any] = {
                "@context": "https://schema.org",
                "@type": "MusicRecording",
                "@id": f"https://open.spotify.com/track/{track_id}",
                "name": title,
                "byArtist": [
                    {"@type": "MusicGroup", "name": artist} for artist in artists
                ],
                "inAlbum": {"@type": "MusicAlbum", "name": album},
            }
            if duration_ms is not None:
                jsonld["duration"] = iso_duration(duration_ms)
            raw_jsonld = json.dumps(jsonld)
        parts.append(f'<script type="application/ld+json">{raw_jsonld}</script>')
    parts.append("</head><body>")
    if include_state:
        if malformed_state:
            raw_state = "this is not base64 json"
        else:
            state_track_id = state_track_id or track_id
            effective_title = state_title if state_title is not None else title
            effective_artists = state_artists if state_artists is not None else artists
            effective_duration = (
                duration_ms if state_duration_ms is ... else state_duration_ms
            )
            entity: dict[str, Any] = {
                "__typename": "Track",
                "id": state_track_id,
                "uri": f"spotify:track:{state_track_id}",
                "name": effective_title,
                "artists": {
                    "items": [
                        {"profile": {"name": artist}} for artist in effective_artists
                    ]
                },
                "albumOfTrack": {"name": album},
            }
            if effective_duration is not None:
                entity["duration"] = {"totalMilliseconds": effective_duration}
            state = {"entities": {"items": {f"spotify:track:{state_track_id}": entity}}}
            raw_state = base64.b64encode(json.dumps(state).encode("utf-8")).decode(
                "ascii"
            )
        parts.append(
            f'<script id="initialState" type="text/plain">{raw_state}</script>'
        )
    parts.append("</body></html>")
    return "".join(parts)


def build_embed_page(
    *,
    track_id: str = TRACK_ID,
    title: str = TRACK_TITLE,
    artists: tuple[str, ...] = TRACK_ARTISTS,
    duration_ms: int | None = TRACK_DURATION_MS,
    include_next_data: bool = True,
) -> str:
    if not include_next_data:
        return '<!doctype html><html><body><div id="embed"></div></body></html>'
    entity: dict[str, Any] = {
        "type": "track",
        "id": track_id,
        "uri": f"spotify:track:{track_id}",
        "name": title,
        "artists": [
            {"name": artist, "profile": {"name": artist}} for artist in artists
        ],
    }
    if duration_ms is not None:
        entity["duration"] = {"totalMilliseconds": duration_ms}
    next_data = {
        "props": {"pageProps": {"state": {"data": {"entity": entity}}}},
    }
    return (
        "<!doctype html><html><body>"
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(next_data)}"
        "</script></body></html>"
    )


def spotify_transport(
    *,
    main_html: str | None = None,
    embed_html: str | None = None,
    main_status: int = 200,
    embed_status: int = 200,
    main_headers: dict[str, str] | None = None,
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/embed/track/"):
            if embed_status != 200:
                return httpx.Response(embed_status, request=request)
            return httpx.Response(
                200,
                text=embed_html or build_embed_page(),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        if path.startswith("/track/"):
            if main_status != 200:
                return httpx.Response(main_status, request=request)
            return httpx.Response(
                200,
                text=main_html or build_spotify_page(),
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def make_http_client(
    settings: Settings,
    transport: httpx.AsyncBaseTransport,
) -> tuple[httpx.AsyncClient, Any]:
    from spotify_resolver.http import LimitedHttpClient

    raw = httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": settings.http_user_agent},
        timeout=None,
        follow_redirects=False,
    )
    limited = LimitedHttpClient(
        raw,
        max_retries=settings.http_max_retries,
        retry_backoff_seconds=settings.http_retry_backoff_seconds,
        retry_after_cap_seconds=settings.http_retry_after_cap_seconds,
        connect_timeout_seconds=settings.http_connect_timeout_seconds,
    )
    return raw, limited
