from __future__ import annotations

import pytest

from spotify_resolver.cache import Cache
from spotify_resolver.matching import ScoringConfig, score_candidate
from spotify_resolver.models import Candidate, SpotifyTrack


def _scored(track: SpotifyTrack):
    return score_candidate(
        track,
        Candidate(
            platform="youtube",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            title=track.title,
            artist="Rick Astley - Topic",
            uploader="Rick Astley - Topic",
            duration_ms=track.duration_ms,
            is_topic=True,
        ),
        "youtube",
        ScoringConfig(),
    )


@pytest.mark.asyncio
async def test_metadata_cache_round_trip(settings, sample_track: SpotifyTrack) -> None:
    cache = Cache(settings.database_path)
    await cache.initialize()
    try:
        assert await cache.get_metadata(sample_track.spotify_id, 1_000.0) is None
        await cache.put_metadata(sample_track, 1_000.0, 60)
        entry = await cache.get_metadata(sample_track.spotify_id, 1_001.0)
        assert entry is not None
        status, track = entry
        assert status == "ok"
        assert track == sample_track
        assert await cache.get_metadata(sample_track.spotify_id, 1_060.0) is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_metadata_not_found_cache(settings) -> None:
    cache = Cache(settings.database_path)
    await cache.initialize()
    try:
        track_id = "4uLU6hMCjMI75M1A2tKUQC"
        await cache.put_metadata_not_found(track_id, 1_000.0, 60)
        entry = await cache.get_metadata(track_id, 1_030.0)
        assert entry == ("not_found", None)
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_spotify_not_found_clears_resolution_cache(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    cache = Cache(settings.database_path)
    await cache.initialize()
    try:
        scored = _scored(sample_track)
        await cache.put_resolution(
            sample_track,
            "youtube",
            "matched",
            candidate=scored,
            best_confidence=scored.score.total,
            now=1_000.0,
            ttl_seconds=3_600,
        )
        await cache.put_metadata_not_found(sample_track.spotify_id, 1_000.0, 60)
        assert (
            await cache.get_resolution(sample_track.spotify_id, "youtube", 1_001.0)
            is None
        )
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_resolution_cache_round_trip(
    settings, sample_track: SpotifyTrack
) -> None:
    cache = Cache(settings.database_path)
    await cache.initialize()
    try:
        scored = _scored(sample_track)
        await cache.put_resolution(
            sample_track,
            "youtube",
            "matched",
            candidate=scored,
            best_confidence=scored.score.total,
            now=1_000.0,
            ttl_seconds=3600,
        )
        cached = await cache.get_resolution(sample_track.spotify_id, "youtube", 1_001.0)
        assert cached is not None
        assert cached.status == "matched"
        assert cached.track == sample_track
        assert cached.candidate is not None
        assert cached.candidate.candidate.url == scored.candidate.url
        assert cached.confidence == pytest.approx(scored.score.total)
        assert (
            await cache.get_resolution(sample_track.spotify_id, "youtube", 4_600.0)
            is None
        )
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_negative_resolution_cache(settings, sample_track: SpotifyTrack) -> None:
    cache = Cache(settings.database_path)
    await cache.initialize()
    try:
        await cache.put_resolution(
            sample_track,
            "soundcloud",
            "not_found",
            candidate=None,
            best_confidence=0.52,
            now=1_000.0,
            ttl_seconds=60,
        )
        cached = await cache.get_resolution(
            sample_track.spotify_id, "soundcloud", 1_010.0
        )
        assert cached is not None
        assert cached.status == "not_found"
        assert cached.track == sample_track
        assert cached.best_confidence == 0.52
        assert cached.candidate is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_prune_removes_expired_rows(settings, sample_track: SpotifyTrack) -> None:
    cache = Cache(settings.database_path)
    await cache.initialize()
    try:
        scored = _scored(sample_track)
        await cache.put_metadata(sample_track, 1_000.0, 10)
        await cache.put_resolution(
            sample_track,
            "youtube",
            "matched",
            candidate=scored,
            best_confidence=scored.score.total,
            now=1_000.0,
            ttl_seconds=10,
        )
        await cache.prune(1_020.0)
        assert await cache.get_metadata(sample_track.spotify_id, 1_020.0) is None
        assert (
            await cache.get_resolution(sample_track.spotify_id, "youtube", 1_020.0)
            is None
        )
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_ping_requires_initialized_cache(settings) -> None:
    cache = Cache(settings.database_path)
    assert await cache.ping() is False
