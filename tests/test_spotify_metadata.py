from __future__ import annotations

import base64
import json

import httpx
import pytest
from conftest import (
    TRACK_ALBUM,
    TRACK_ARTISTS,
    TRACK_DURATION_MS,
    TRACK_ID,
    TRACK_TITLE,
    build_embed_page,
    build_spotify_page,
    make_http_client,
    spotify_transport,
)

from spotify_resolver.errors import (
    SpotifyMetadataError,
    SpotifyNotFound,
    SpotifyRateLimited,
    SpotifyTimeout,
)
from spotify_resolver.spotify import (
    SpotifyClient,
    SpotifyMetadataIncomplete,
    parse_spotify_page,
)


def test_parse_complete_page() -> None:
    track = parse_spotify_page(build_spotify_page(), TRACK_ID)
    assert track.spotify_id == TRACK_ID
    assert track.title == TRACK_TITLE
    assert track.artists == TRACK_ARTISTS
    assert track.album == TRACK_ALBUM
    assert track.duration_ms == TRACK_DURATION_MS
    assert track.duration_precision == "ms"


def test_jsonld_only_page() -> None:
    html = build_spotify_page(include_meta=False, include_state=False)
    track = parse_spotify_page(html, TRACK_ID)
    assert track.title == TRACK_TITLE
    assert track.artists == TRACK_ARTISTS
    assert track.album == TRACK_ALBUM
    assert track.duration_ms == TRACK_DURATION_MS
    assert track.duration_precision == "ms"


def test_jsonld_whole_second_duration_uses_second_precision() -> None:
    html = build_spotify_page(
        include_meta=False,
        include_state=False,
        duration_ms=213_000,
    )
    track = parse_spotify_page(html, TRACK_ID)
    assert track.duration_ms == 213_000
    assert track.duration_precision == "s"


def test_meta_only_page_uses_second_precision() -> None:
    html = build_spotify_page(include_jsonld=False, include_state=False)
    track = parse_spotify_page(html, TRACK_ID)
    assert track.title == TRACK_TITLE
    assert track.artists == TRACK_ARTISTS
    assert track.album == TRACK_ALBUM
    assert track.duration_ms == 214_000
    assert track.duration_precision == "s"


def test_initial_state_only_page() -> None:
    html = build_spotify_page(include_jsonld=False, include_meta=False)
    track = parse_spotify_page(html, TRACK_ID)
    assert track.title == TRACK_TITLE
    assert track.artists == TRACK_ARTISTS
    assert track.album == TRACK_ALBUM
    assert track.duration_ms == TRACK_DURATION_MS


def test_next_data_embed_page() -> None:
    track = parse_spotify_page(build_embed_page(), TRACK_ID)
    assert track.title == TRACK_TITLE
    assert track.artists == TRACK_ARTISTS
    assert track.duration_ms == TRACK_DURATION_MS
    assert track.album is None


def test_partial_state_without_duration() -> None:
    html = build_spotify_page(
        include_jsonld=False,
        include_meta=False,
        state_duration_ms=None,
    )
    track = parse_spotify_page(html, TRACK_ID)
    assert track.title == TRACK_TITLE
    assert track.duration_ms is None
    assert track.duration_precision is None


def test_malformed_embedded_json_falls_back_to_meta() -> None:
    html = build_spotify_page(
        include_jsonld=False,
        malformed_state=True,
        include_meta=True,
    )
    track = parse_spotify_page(html, TRACK_ID)
    assert track.title == TRACK_TITLE
    assert track.artists == TRACK_ARTISTS
    assert track.duration_ms == 214_000


def test_malformed_jsonld_does_not_break_other_sources() -> None:
    html = build_spotify_page(malformed_jsonld=True)
    track = parse_spotify_page(html, TRACK_ID)
    assert track.title == TRACK_TITLE
    assert track.duration_ms == TRACK_DURATION_MS


def test_missing_artist_is_incomplete() -> None:
    html = build_spotify_page(
        include_jsonld=False, include_state=False, include_meta=False
    )
    with pytest.raises(SpotifyMetadataIncomplete):
        parse_spotify_page(html, TRACK_ID)


def test_wrong_id_entity_is_ignored() -> None:
    wrong_id = "a" * 22
    html = build_spotify_page(state_track_id=wrong_id, state_title="Wrong song")
    track = parse_spotify_page(html, TRACK_ID)
    assert track.title == TRACK_TITLE
    assert track.title != "Wrong song"


def test_initial_state_first_and_other_artists() -> None:
    entity = {
        "__typename": "Track",
        "uri": f"spotify:track:{TRACK_ID}",
        "name": TRACK_TITLE,
        "firstArtist": {"items": [{"profile": {"name": "Rick Astley"}}]},
        "otherArtists": {"items": [{"profile": {"name": "Guest Artist"}}]},
        "albumOfTrack": {"name": TRACK_ALBUM},
        "duration": {"totalMilliseconds": TRACK_DURATION_MS},
    }
    state = {"entities": {"items": {f"spotify:track:{TRACK_ID}": entity}}}
    raw_state = base64.b64encode(json.dumps(state).encode("utf-8")).decode("ascii")
    html = (
        "<html><head></head><body>"
        f'<script id="initialState" type="text/plain">{raw_state}</script>'
        "</body></html>"
    )
    track = parse_spotify_page(html, TRACK_ID)
    assert track.artists == ("Rick Astley", "Guest Artist")
    assert track.album == TRACK_ALBUM
    assert track.duration_ms == TRACK_DURATION_MS


def test_artists_are_deduplicated() -> None:
    html = build_spotify_page(
        include_jsonld=False,
        include_meta=False,
        state_artists=("Rick Astley", "Rick Astley", "R. Astley"),
    )
    track = parse_spotify_page(html, TRACK_ID)
    assert track.artists == ("Rick Astley", "R. Astley")


@pytest.mark.asyncio
async def test_client_uses_embed_fallback(settings) -> None:
    transport = spotify_transport(
        main_html=build_spotify_page(shell=True),
        embed_html=build_embed_page(),
    )
    _raw, http = make_http_client(settings, transport)
    try:
        track = await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()
    assert track.spotify_id == TRACK_ID
    assert track.title == TRACK_TITLE
    assert track.duration_ms == TRACK_DURATION_MS


@pytest.mark.asyncio
async def test_client_embed_404_after_incomplete_main_is_metadata_error(
    settings,
) -> None:
    transport = spotify_transport(
        main_html=build_spotify_page(shell=True),
        embed_status=404,
    )
    _raw, http = make_http_client(settings, transport)
    try:
        with pytest.raises(SpotifyMetadataError):
            await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_client_enriches_missing_duration_from_embed(settings) -> None:
    transport = spotify_transport(
        main_html=build_spotify_page(duration_ms=None, include_duration_meta=False),
        embed_html=build_embed_page(duration_ms=213_500),
    )
    _raw, http = make_http_client(settings, transport)
    try:
        track = await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()
    assert track.title == TRACK_TITLE
    assert track.duration_ms == 213_500
    assert track.duration_precision == "ms"


@pytest.mark.asyncio
async def test_client_maps_404(settings) -> None:
    transport = spotify_transport(main_status=404)
    _raw, http = make_http_client(settings, transport)
    try:
        with pytest.raises(SpotifyNotFound):
            await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_client_maps_429(settings) -> None:
    transport = spotify_transport(main_status=429)
    _raw, http = make_http_client(settings, transport)
    try:
        with pytest.raises(SpotifyRateLimited):
            await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_client_maps_timeout(make_settings) -> None:
    settings = make_settings(http_max_retries=0)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(SpotifyTimeout):
            await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_client_rejects_redirect_outside_spotify(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://evil.example/track/abc"},
            request=request,
        )

    _raw, http = make_http_client(settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(SpotifyMetadataError):
            await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_client_rejects_oversized_page(make_settings) -> None:
    settings = make_settings(spotify_max_bytes=100)
    transport = spotify_transport(main_html=build_spotify_page())
    _raw, http = make_http_client(settings, transport)
    try:
        with pytest.raises(SpotifyMetadataError):
            await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_client_fails_when_main_and_embed_are_unusable(settings) -> None:
    transport = spotify_transport(
        main_html=build_spotify_page(shell=True),
        embed_html=build_embed_page(include_next_data=False),
    )
    _raw, http = make_http_client(settings, transport)
    try:
        with pytest.raises(SpotifyMetadataError):
            await SpotifyClient(http, settings).get_track(TRACK_ID)
    finally:
        await http.aclose()
