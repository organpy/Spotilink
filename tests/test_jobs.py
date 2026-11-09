from __future__ import annotations

import asyncio
import sqlite3

from conftest import MockClock, make_http_client
from spotify_fakes import FakeSpotifyPlaylists, track_item

from spotify_resolver.cache import SCHEMA_VERSION, Cache
from spotify_resolver.jobs import JobManager, JobStore, TrackMatcher
from spotify_resolver.playlists.spotify_downloader_adapter import SpotifyDownloaderExtractor
from spotify_resolver.playlists.spotify_embed import SpotifyEmbedExtractor
from spotify_resolver.playlists.spotify_playlist import SpotifyPlaylistService
from spotify_resolver.playlists.spotify_webplayer import SpotifyWebPlayerExtractor
from spotify_resolver.playlists.youtube_playlist import YouTubePlaylistExtractor
from spotify_resolver.providers.ytmusic import YTMusicMatcher
from spotify_resolver.urls import classify_url
from test_api_v2 import SmartYTMusic

PLAYLIST = classify_url("https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U")


async def build(settings, fake, ytm, clock, *, store=None, cache=None):
    raw, http = make_http_client(settings, fake.transport())
    cache = cache or Cache(settings.database_path)
    await cache.initialize()
    store = store or JobStore(settings.database_path, clock=clock)
    await store.initialize()
    service = SpotifyPlaylistService(
        settings,
        primary=SpotifyDownloaderExtractor(http, settings, clock=clock),
        secondary=SpotifyWebPlayerExtractor(http, settings, clock=clock),
        embed=SpotifyEmbedExtractor(http, settings),
    )
    matcher = YTMusicMatcher.from_settings(ytm, settings)
    manager = JobManager(
        settings, store, spotify=service, youtube=YouTubePlaylistExtractor(settings),
        track_matcher=TrackMatcher(matcher, cache, settings, clock=clock),
    )
    return manager, store, cache, raw


async def test_two_managers_never_process_the_same_job(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True, yt_music_retry_backoff_seconds=0.0)
    clock = MockClock()
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 6)])
    ytm = SmartYTMusic()
    m1, store1, cache1, raw1 = await build(settings, fake, ytm, clock)
    m2, store2, cache2, raw2 = await build(settings, fake, ytm, clock)
    try:
        job, _ = await m1.submit(PLAYLIST, "u")
        results = await asyncio.gather(m1.run_once(), m2.run_once())
        assert sorted(results) == [False, True]  # exactly one claimed it
        row = await store1.get(job["job_id"])
        assert row["status"] == "completed" and row["attempts"] == 1
        assert len(ytm.calls) == 5
    finally:
        for obj in (store1, store2, cache1, cache2):
            await obj.close()
        await raw1.aclose()
        await raw2.aclose()


async def test_interrupted_job_is_recovered_and_resumes(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True, yt_music_retry_backoff_seconds=0.0)
    clock = MockClock()
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 7)])
    ytm = SmartYTMusic()
    m1, store, cache, raw = await build(settings, fake, ytm, clock)
    try:
        job, _ = await m1.submit(PLAYLIST, "u")
        job_id = job["job_id"]
        # Simulate a crash mid-matching: extraction saved, 2 tracks done, lease still held.
        claimed = await store.claim("dead-worker", settings.job_lease_seconds)
        extraction = await m1._extract(claimed)
        await store.save_extraction(job_id, extraction, m1._initial_rows(extraction))
        pending = await store.pending_tracks(job_id)
        for row in pending[:2]:
            await store.update_track(job_id, row["position"], (await m1._track_matcher.resolve(
                __import__("spotify_resolver.jobs", fromlist=["_entry_from_row"])._entry_from_row(row))))
        searched_before = len(ytm.calls)

        # While the lease is valid nobody else may take it.
        assert await m1.run_once() is False
        clock.advance(settings.job_lease_seconds + 1)
        assert await m1.run_once() is True

        row = await store.get(job_id)
        assert row["status"] == "completed" and row["attempts"] == 2
        counts = await store.counts(job_id)
        assert counts["matched"] == 6 and counts["pending"] == 0
        assert len(ytm.calls) - searched_before == 4  # finished tracks are not redone
    finally:
        await store.close()
        await cache.close()
        await raw.aclose()


async def test_repeatedly_interrupted_job_is_failed(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True)
    clock = MockClock()
    m, store, cache, raw = await build(settings, FakeSpotifyPlaylists([track_item(1)]), SmartYTMusic(), clock)
    try:
        job, _ = await m.submit(PLAYLIST, "u")
        for _ in range(4):
            assert await store.claim("w", settings.job_lease_seconds)
            clock.advance(settings.job_lease_seconds + 1)
        await m.run_once()
        row = await store.get(job["job_id"])
        assert row["status"] == "failed" and row["error_code"] == "job_interrupted"
    finally:
        await store.close()
        await cache.close()
        await raw.aclose()


async def test_incomplete_playlist_cache_expires_quickly(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True, playlist_partial_cache_ttl=600, playlist_cache_ttl=86_400)
    clock = MockClock()
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 4)], total=9)
    m, store, cache, raw = await build(settings, fake, SmartYTMusic(), clock)
    try:
        job, _ = await m.submit(PLAYLIST, "u")
        await m.run_once()
        same, reused = await m.submit(PLAYLIST, "u")
        assert reused and same["job_id"] == job["job_id"]
        clock.advance(601)
        fresh, reused = await m.submit(PLAYLIST, "u")
        assert not reused and fresh["job_id"] != job["job_id"]
        assert await store.get_playlist("spotify", PLAYLIST.identifier, 1000) is None
    finally:
        await store.close()
        await cache.close()
        await raw.aclose()


async def test_failed_jobs_are_not_reused(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True)
    clock = MockClock()
    fake = FakeSpotifyPlaylists([])
    fake.not_found = True
    m, store, cache, raw = await build(settings, fake, SmartYTMusic(), clock)
    try:
        first, _ = await m.submit(PLAYLIST, "u")
        await m.run_once()
        second, reused = await m.submit(PLAYLIST, "u")
        assert not reused and second["job_id"] != first["job_id"]
    finally:
        await store.close()
        await cache.close()
        await raw.aclose()


async def test_prune_removes_expired_jobs_and_tracks(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True, job_ttl_seconds=100)
    clock = MockClock()
    m, store, cache, raw = await build(settings, FakeSpotifyPlaylists([track_item(1)]), SmartYTMusic(), clock)
    try:
        job, _ = await m.submit(PLAYLIST, "u")
        await m.run_once()
        assert await store.get(job["job_id"]) is not None
        clock.advance(101)
        assert await store.prune() == 1
        assert await store.get(job["job_id"]) is None
        assert await store.tracks(job["job_id"], 0, 10) == []
    finally:
        await store.close()
        await cache.close()
        await raw.aclose()


async def test_transient_failures_are_not_cached_as_unmatched(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True, yt_music_retry_backoff_seconds=0.0)
    clock = MockClock()
    ytm = SmartYTMusic(failing={1})
    m, store, cache, raw = await build(settings, FakeSpotifyPlaylists([track_item(1)]), ytm, clock)
    try:
        job, _ = await m.submit(PLAYLIST, "u")
        await m.run_once()
        from spotify_fakes import track_id

        assert (await store.counts(job["job_id"]))["failed"] == 1
        assert await cache.get_resolution(track_id(1), "youtube", clock()) is None
    finally:
        await store.close()
        await cache.close()
        await raw.aclose()


async def test_cache_migration_preserves_existing_rows(tmp_path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE resolutions (
            spotify_id TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL, url TEXT,
            confidence REAL, best_confidence REAL, candidate_json TEXT, title TEXT NOT NULL,
            artists_json TEXT NOT NULL, album TEXT, duration_ms INTEGER, duration_precision TEXT,
            resolved_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            PRIMARY KEY (spotify_id, platform));
        INSERT INTO resolutions VALUES ('abc','youtube','not_found',NULL,NULL,0.4,NULL,'T','["A"]',
            NULL,1000,'ms',1,9999999999);
        PRAGMA user_version=1;
        """
    )
    conn.commit()
    conn.close()
    cache = Cache(path)
    await cache.initialize()
    await cache.initialize()
    try:
        row = await cache.get_resolution("abc", "youtube", 100.0)
        assert row is not None and row.status == "not_found" and row.provider is None
    finally:
        await cache.close()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


async def test_job_with_failed_tracks_is_not_reused(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True, yt_music_retry_backoff_seconds=0.0)
    clock = MockClock()
    ytm = SmartYTMusic(failing={1})
    m, store, cache, raw = await build(settings, FakeSpotifyPlaylists([track_item(1), track_item(2)]), ytm, clock)
    try:
        first, _ = await m.submit(PLAYLIST, "u")
        await m.run_once()
        assert (await store.get(first["job_id"]))["status"] == "partial"
        second, reused = await m.submit(PLAYLIST, "u")
        assert not reused and second["job_id"] != first["job_id"]
        ytm.failing.clear()
        await m.run_once()
        counts = await store.counts(second["job_id"])
        assert counts["matched"] == 2 and counts["failed"] == 0
        # Track 2 was searched once (first run) and is served from cache on the retry.
        assert sum(q.endswith(" 2") for q in ytm.calls) == 1
        assert sum(q.endswith(" 1") for q in ytm.calls) == 5  # 4 failed attempts + 1 retry
    finally:
        await store.close()
        await cache.close()
        await raw.aclose()
