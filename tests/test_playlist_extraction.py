from __future__ import annotations

import pytest
from conftest import MockClock, make_http_client
from spotify_fakes import (
    BUNDLE_URL,
    WEBPLAYER_HASH,
    FakeSpotifyPlaylists,
    track_id,
    track_item,
)

from spotify_resolver.playlists.models import (
    PlaylistEmpty,
    PlaylistNotFound,
    PlaylistSchemaChanged,
    PlaylistUpstreamBlocked,
)
from spotify_resolver.playlists.spotify_downloader_adapter import SpotifyDownloaderExtractor
from spotify_resolver.playlists.spotify_embed import SpotifyEmbedExtractor
from spotify_resolver.playlists.spotify_playlist import SpotifyPlaylistService
from spotify_resolver.playlists.spotify_webplayer import (
    SpotifyWebPlayerExtractor,
    find_bundle_urls,
    find_query_hash,
)

PID = "37i9dQZF1DWXRqgorJj26U"


@pytest.fixture
async def env(settings):
    clients = []

    def build(fake: FakeSpotifyPlaylists, **overrides):
        s = settings if not overrides else __import__("dataclasses").replace(settings, **overrides)
        raw, http = make_http_client(s, fake.transport())
        clients.append(raw)
        clock = MockClock(1_700_000_000.0)
        return (
            SpotifyDownloaderExtractor(http, s, clock=clock),
            SpotifyWebPlayerExtractor(http, s, clock=clock),
            SpotifyEmbedExtractor(http, s),
            s,
        )

    yield build
    for raw in clients:
        await raw.aclose()


@pytest.mark.parametrize("size", [10, 100, 101, 250, 1000])
async def test_both_methods_paginate_to_reported_total(env, size: int) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, size + 1)])
    method_a, method_b, _, _ = env(fake)
    for extractor in (method_a, method_b):
        result = await extractor.extract(PID, 5000)
        assert result.extraction_complete is True
        assert result.reported_total == size == result.extracted_total
        assert [e.position for e in result.entries] == list(range(1, size + 1))
        assert result.entries[0].source_id == track_id(1)
        assert result.stats.pages == max(1, -(-size // 100))


async def test_metadata_is_normalized(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(1, artists=("A", "B"))])
    method_a, method_b, _, _ = env(fake)
    for extractor in (method_a, method_b):
        result = await extractor.extract(PID, 100)
        entry = result.entries[0]
        assert result.title == "Test Playlist"
        assert result.owner == "Owner"
        assert result.description == "A test & playlist"
        assert entry.artists == ("A", "B")
        assert entry.duration_ms == 180_001
        assert entry.album == "Album 1"
        assert entry.url == f"https://open.spotify.com/track/{track_id(1)}"


async def test_duplicates_and_removed_items_are_preserved(env) -> None:
    removed = {
        "itemV2": {
            "data": {"__typename": "Track", "uri": f"spotify:track:{track_id(9)}", "artists": {"items": []}}
        }
    }
    episode = {"itemV2": {"data": {"__typename": "Episode", "uri": "spotify:episode:x", "name": "Pod"}}}
    items = [track_item(1), track_item(2), track_item(1), removed, episode, {"itemV2": None}]
    fake = FakeSpotifyPlaylists(items)
    method_a, method_b, _, _ = env(fake)
    for extractor in (method_a, method_b):
        result = await extractor.extract(PID, 100)
        assert result.extraction_complete
        assert [e.source_id for e in result.entries[:3]] == [track_id(1), track_id(2), track_id(1)]
        reasons = [e.unavailable_reason for e in result.entries[3:]]
        assert reasons == ["removed", "podcast_episode", "missing_item"]


async def test_max_tracks_truncates_and_reports_incomplete(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 301)])
    method_a, method_b, _, _ = env(fake)
    for extractor in (method_a, method_b):
        result = await extractor.extract(PID, 150)
        assert result.extracted_total == 150
        assert result.truncated and not result.extraction_complete
        assert result.incomplete_reason == "max_tracks"


async def test_count_mismatch_is_not_complete(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 51)], total=60)
    method_a, method_b, _, _ = env(fake)
    for extractor in (method_a, method_b):
        result = await extractor.extract(PID, 100)
        assert result.extracted_total == 50
        assert result.extraction_complete is False
        assert result.incomplete_reason == "count_mismatch"


async def test_not_found_and_invalid(env) -> None:
    fake = FakeSpotifyPlaylists([])
    fake.not_found = True
    method_a, method_b, embed, _ = env(fake)
    for extractor in (method_a, method_b, embed):
        with pytest.raises(PlaylistNotFound):
            await extractor.extract(PID, 100)
    fake.not_found = False
    fake.generic_error = "Failed to fetch playlist, status code: 400 BAD_REQUEST"
    for extractor in (method_a, method_b):
        with pytest.raises(PlaylistNotFound):
            await extractor.extract(PID, 100)


async def test_graphql_schema_change_fails_clearly(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(1)])
    fake.graphql_errors = True
    method_a, method_b, _, _ = env(fake)
    for extractor in (method_a, method_b):
        with pytest.raises(PlaylistSchemaChanged):
            await extractor.extract(PID, 100)


async def test_expired_token_is_renewed(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(1)])
    method_a, method_b, _, _ = env(fake)
    await method_a.extract(PID, 100)
    fake.valid_tokens = {"tok-2"}  # tok-1 now rejected with 401
    result = await method_a.extract(PID, 100)
    assert result.extraction_complete
    assert fake.token_counter == 2


async def test_webplayer_discovers_and_rediscovers_hash(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(1)])
    _, method_b, _, _ = env(fake)
    await method_b.extract(PID, 100)
    assert any("open.spotifycdn.com" in r for r in fake.requests)
    # Spotify rotates the hash: old one now returns HTTP 412.
    new_hash = "c" * 64
    fake.accepted_hashes = {new_hash}
    fake.bundle_hash = new_hash
    result = await method_b.extract(PID, 100)
    assert result.extraction_complete
    assert sum("open.spotifycdn.com" in r for r in fake.requests) == 2


async def test_webplayer_unknown_hash_fails_with_schema_error(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(1)])
    fake.accepted_hashes = {"d" * 64}
    _, method_b, _, _ = env(fake)
    with pytest.raises(PlaylistSchemaChanged):
        await method_b.extract(PID, 100)


async def test_method_a_hash_rejected_is_schema_error(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(1)])
    fake.accepted_hashes = {WEBPLAYER_HASH}
    method_a, _, _, _ = env(fake)
    with pytest.raises(PlaylistSchemaChanged):
        await method_a.extract(PID, 100)


def test_bundle_and_hash_parsers() -> None:
    page = f'<script src="{BUNDLE_URL}"></script><script src="https://evil.example/x.js">'
    assert find_bundle_urls(page) == [BUNDLE_URL]
    assert find_query_hash('"queryPlaylist","query","' + "a" * 64 + '"') == "a" * 64
    assert find_query_hash('"fetchPlaylist","query","' + "a" * 64 + '"') is None


async def test_mid_pagination_failure_returns_partial(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 251)])
    fake.fail_at_offset = {200: 500}
    method_a, _, _, _ = env(fake)
    with pytest.raises(Exception) as info:
        await method_a.extract(PID, 1000)
    partial = info.value.partial
    assert partial is not None and partial.extracted_total == 200
    assert partial.extraction_complete is False


async def test_embed_is_always_partial(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 151)])
    _, _, embed, _ = env(fake)
    result = await embed.extract(PID, 1000)
    assert result.extracted_total == 100
    assert result.extraction_complete is False
    assert result.incomplete_reason == "embed_first_page_only"


# ---------------------------------------------------------------- orchestrator


def _service(settings, a, b, embed):
    return SpotifyPlaylistService(settings, primary=a, secondary=b, embed=embed)


async def test_orchestrator_uses_primary_first(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 11)])
    a, b, embed, s = env(fake)
    result = await _service(s, a, b, embed).extract(PID, 100)
    assert result.extractor == "spotify_downloader"
    assert result.fallback_errors == ()


async def test_orchestrator_falls_back_to_webplayer(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 11)])
    fake.accepted_hashes = {WEBPLAYER_HASH}  # Method A's hash retired
    a, b, embed, s = env(fake)
    result = await _service(s, a, b, embed).extract(PID, 100)
    assert result.extractor == "webplayer"
    assert result.extraction_complete
    assert result.fallback_errors and "spotify_downloader" in result.fallback_errors[0]


async def test_orchestrator_skips_webplayer_when_blocked(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 11)])
    fake.fail_at_offset = {0: 429}
    a, b, embed, s = env(fake)
    result = await _service(s, a, b, embed).extract(PID, 100)
    assert result.extractor == "spotify_embed"
    assert result.extraction_complete is False
    assert any("skipped" in e for e in result.fallback_errors)
    assert not any("/pathfinder/v2" in r for r in fake.requests)


async def test_orchestrator_blocked_without_embed_raises(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(1)])
    fake.fail_at_offset = {0: 429}
    a, b, embed, s = env(fake, spotify_embed_partial_fallback=False)
    with pytest.raises(PlaylistUpstreamBlocked):
        await _service(s, a, b, embed).extract(PID, 100)


async def test_orchestrator_not_found_is_terminal(env) -> None:
    fake = FakeSpotifyPlaylists([])
    fake.not_found = True
    a, b, embed, s = env(fake)
    with pytest.raises(PlaylistNotFound):
        await _service(s, a, b, embed).extract(PID, 100)
    assert not any("/pathfinder/v2" in r for r in fake.requests)


async def test_orchestrator_empty_playlist(env) -> None:
    fake = FakeSpotifyPlaylists([])
    a, b, embed, s = env(fake)
    with pytest.raises(PlaylistEmpty):
        await _service(s, a, b, embed).extract(PID, 100)


async def test_orchestrator_keeps_best_partial(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 251)])
    fake.fail_at_offset = {200: 500}
    a, b, embed, s = env(fake)
    result = await _service(s, a, b, embed).extract(PID, 1000)
    # Method B pages differently (also 100), fails at the same offset; the
    # best partial (200 entries) beats the embed's 100.
    assert result.extracted_total == 200
    assert result.extraction_complete is False


async def test_webplayer_mode_prefers_method_b(env) -> None:
    fake = FakeSpotifyPlaylists([track_item(1)])
    a, b, embed, s = env(fake, spotify_playlist_extractor="webplayer")
    result = await _service(s, a, b, embed).extract(PID, 100)
    assert result.extractor == "webplayer"
