"""Persistent playlist jobs backed by SQLite.

Design
------
* `jobs` holds one row per playlist job; `job_tracks` holds one row per
  playlist position, so results are readable while a job is still running and
  survive restarts. Progress counters are aggregated from `job_tracks`.
* Workers claim jobs with a lease inside `BEGIN IMMEDIATE`, which serialises
  claims across processes sharing the database: two uvicorn workers never
  process the same job. Leases are renewed by a heartbeat; a job whose lease
  expired (crash, restart, OOM kill) is reclaimed and resumes from the first
  pending track. A job that keeps dying is failed after a few attempts.
* `playlist_cache` stores extracted playlists. Complete extractions are kept for
  PLAYLIST_CACHE_TTL; incomplete ones only for PLAYLIST_PARTIAL_CACHE_TTL so an
  incomplete extraction is never treated as permanently complete.
* Spotify tracks are matched with ytmusicapi only, through the shared
  `resolutions` cache, so a track appearing in many playlists is searched once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .cache import Cache
from .config import Settings
from .errors import ProviderRateLimited
from .matching import score_candidate
from .models import ScoredCandidate, SpotifyTrack
from .playlists.models import (
    ExtractionStats,
    PlaylistEntry,
    PlaylistError,
    PlaylistExtraction,
)
from .playlists.spotify_playlist import SpotifyPlaylistService
from .playlists.youtube_playlist import YouTubePlaylistExtractor
from .providers.ytmusic import PROVIDER_NAME as YTMUSIC, MatchOutcome, YTMusicMatcher
from .urls import ClassifiedInput, InputKind

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "extracting", "matching")
FINAL_STATUSES = ("completed", "partial", "failed")
TRACK_STATUSES = ("pending", "matched", "uncertain", "unmatched", "unavailable", "failed")
_MAX_JOB_ATTEMPTS = 3
_MAX_TRACK_RETRIES = 2
_CONSECUTIVE_FAILURE_LIMIT = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlist_cache (
    source TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    max_tracks INTEGER NOT NULL,
    title TEXT,
    description TEXT,
    owner TEXT,
    reported_total INTEGER,
    extraction_complete INTEGER NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0,
    incomplete_reason TEXT,
    extractor TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (source, playlist_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    input_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('queued', 'extracting', 'matching', 'completed', 'partial', 'failed')),
    max_tracks INTEGER NOT NULL,
    title TEXT,
    description TEXT,
    owner TEXT,
    reported_total INTEGER,
    extraction_complete INTEGER,
    truncated INTEGER NOT NULL DEFAULT 0,
    incomplete_reason TEXT,
    extractor TEXT,
    resolver TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    warnings_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_playlist ON jobs (source, playlist_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_expiry ON jobs (expires_at);

CREATE TABLE IF NOT EXISTS job_tracks (
    job_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    source_id TEXT,
    title TEXT,
    artists_json TEXT NOT NULL,
    duration_ms INTEGER,
    album TEXT,
    isrc TEXT,
    source_url TEXT,
    status TEXT NOT NULL,
    youtube_id TEXT,
    youtube_url TEXT,
    confidence REAL,
    provider TEXT,
    match_title TEXT,
    match_artist TEXT,
    detail TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (job_id, position)
);

CREATE INDEX IF NOT EXISTS idx_job_tracks_status ON job_tracks (job_id, status);
"""


class QueueFull(Exception):
    pass


@dataclass(frozen=True, slots=True)
class TrackResult:
    status: str
    youtube_id: str | None = None
    youtube_url: str | None = None
    confidence: float | None = None
    provider: str | None = None
    match_title: str | None = None
    match_artist: str | None = None
    detail: str | None = None


def new_job_id() -> str:
    return "job_" + secrets.token_urlsafe(12)


def _youtube_id(url: str | None) -> str | None:
    if url and "watch?v=" in url:
        return url.rsplit("watch?v=", 1)[1][:11]
    return None


class JobStore:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self._path = str(path)
        self._clock = clock
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._db is not None:
            return
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path, isolation_level=None)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.executescript(_SCHEMA)
        except Exception:
            await db.close()
            raise
        self._db = db
        if self._path != ":memory:":
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def _now(self) -> int:
        return int(self._clock())

    def _require(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("JobStore has not been initialized")
        return self._db

    async def _fetchone(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        async with self._lock:
            cursor = await self._require().execute(sql, params)
            row = await cursor.fetchone()
            await cursor.close()
        return row

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            cursor = await self._require().execute(sql, params)
            rows = await cursor.fetchall()
            await cursor.close()
        return list(rows)

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        async with self._lock:
            cursor = await self._require().execute(sql, params)
            count = cursor.rowcount
            await cursor.close()
        return count

    # ------------------------------------------------------------------ jobs

    async def find_reusable(
        self,
        source: str,
        playlist_id: str,
        max_tracks: int,
        *,
        complete_ttl: int,
        partial_ttl: int,
    ) -> aiosqlite.Row | None:
        now = self._now()
        return await self._fetchone(
            """
            SELECT * FROM jobs
            WHERE source = ? AND playlist_id = ? AND max_tracks = ?
              AND expires_at > ?
              AND (
                status IN ('queued', 'extracting', 'matching')
                OR (status = 'completed' AND finished_at > ?)
                OR (status = 'partial' AND finished_at > ?)
              )
            ORDER BY created_at DESC LIMIT 1
            """,
            (source, playlist_id, max_tracks, now, now - complete_ttl, now - partial_ttl),
        )

    async def count_active(self) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'extracting', 'matching')"
        )
        return int(row[0]) if row else 0

    async def create(
        self, job_id: str, classified: ClassifiedInput, input_url: str, max_tracks: int, ttl: int
    ) -> None:
        now = self._now()
        await self._execute(
            """
            INSERT INTO jobs (job_id, source, playlist_id, input_url, status, max_tracks,
                              created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (
                job_id,
                classified.source,
                classified.identifier,
                input_url[:2048],
                max_tracks,
                now,
                now,
                now + ttl,
            ),
        )

    async def get(self, job_id: str) -> aiosqlite.Row | None:
        return await self._fetchone("SELECT * FROM jobs WHERE job_id = ?", (job_id,))

    async def counts(self, job_id: str) -> dict[str, int]:
        rows = await self._fetchall(
            "SELECT status, COUNT(*) AS n FROM job_tracks WHERE job_id = ? GROUP BY status",
            (job_id,),
        )
        counts = {status: 0 for status in TRACK_STATUSES}
        for row in rows:
            counts[row["status"]] = int(row["n"])
        return counts

    async def claim(self, owner: str, lease_seconds: int) -> aiosqlite.Row | None:
        """Atomically lease the oldest runnable job (queued or lease-expired)."""
        now = self._now()
        async with self._lock:
            db = self._require()
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT job_id, attempts FROM jobs
                    WHERE status = 'queued'
                       OR (status IN ('extracting', 'matching')
                           AND (lease_until IS NULL OR lease_until < ?))
                    ORDER BY created_at LIMIT 1
                    """,
                    (now,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    await db.execute("COMMIT")
                    return None
                job_id = row["job_id"]
                await db.execute(
                    """
                    UPDATE jobs SET lease_owner = ?, lease_until = ?, attempts = attempts + 1,
                        started_at = COALESCE(started_at, ?), updated_at = ?
                    WHERE job_id = ?
                    """,
                    (owner, now + lease_seconds, now, now, job_id),
                )
                cursor = await db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
                claimed = await cursor.fetchone()
                await cursor.close()
                await db.execute("COMMIT")
                return claimed
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def renew(self, job_id: str, owner: str, lease_seconds: int) -> bool:
        now = self._now()
        changed = await self._execute(
            "UPDATE jobs SET lease_until = ?, updated_at = ? WHERE job_id = ? AND lease_owner = ?",
            (now + lease_seconds, now, job_id, owner),
        )
        return changed > 0

    async def release_owner(self, owner: str) -> None:
        await self._execute(
            "UPDATE jobs SET lease_until = 0 WHERE lease_owner = ? AND status IN "
            "('extracting', 'matching')",
            (owner,),
        )

    async def set_status(self, job_id: str, status: str) -> None:
        await self._execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
            (status, self._now(), job_id),
        )

    async def save_extraction(
        self, job_id: str, extraction: PlaylistExtraction, tracks: list[tuple[PlaylistEntry, TrackResult]]
    ) -> None:
        now = self._now()
        async with self._lock:
            db = self._require()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute("DELETE FROM job_tracks WHERE job_id = ?", (job_id,))
                await db.executemany(
                    """
                    INSERT INTO job_tracks (
                        job_id, position, source_id, title, artists_json, duration_ms, album,
                        isrc, source_url, status, youtube_id, youtube_url, confidence, provider,
                        match_title, match_artist, detail, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            job_id,
                            entry.position,
                            entry.source_id,
                            entry.title,
                            json.dumps(list(entry.artists)),
                            entry.duration_ms,
                            entry.album,
                            entry.isrc,
                            entry.url,
                            result.status,
                            result.youtube_id,
                            result.youtube_url,
                            result.confidence,
                            result.provider,
                            result.match_title,
                            result.match_artist,
                            result.detail,
                            now,
                        )
                        for entry, result in tracks
                    ],
                )
                await db.execute(
                    """
                    UPDATE jobs SET title = ?, description = ?, owner = ?, reported_total = ?,
                        extraction_complete = ?, truncated = ?, incomplete_reason = ?,
                        extractor = ?, total = ?, warnings_json = ?, status = 'matching',
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        extraction.title,
                        extraction.description,
                        extraction.owner,
                        extraction.reported_total,
                        int(extraction.extraction_complete),
                        int(extraction.truncated),
                        extraction.incomplete_reason,
                        extraction.extractor,
                        len(tracks),
                        json.dumps(list(extraction.fallback_errors)) if extraction.fallback_errors else None,
                        now,
                        job_id,
                    ),
                )
                await db.execute("COMMIT")
            except BaseException:
                await db.execute("ROLLBACK")
                raise

    async def pending_tracks(self, job_id: str) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM job_tracks WHERE job_id = ? AND status = 'pending' ORDER BY position",
            (job_id,),
        )

    async def update_track(self, job_id: str, position: int, result: TrackResult) -> None:
        await self._execute(
            """
            UPDATE job_tracks SET status = ?, youtube_id = ?, youtube_url = ?, confidence = ?,
                provider = ?, match_title = ?, match_artist = ?, detail = ?, updated_at = ?
            WHERE job_id = ? AND position = ?
            """,
            (
                result.status,
                result.youtube_id,
                result.youtube_url,
                result.confidence,
                result.provider,
                result.match_title,
                result.match_artist,
                result.detail,
                self._now(),
                job_id,
                position,
            ),
        )

    async def tracks(self, job_id: str, offset: int, limit: int) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM job_tracks WHERE job_id = ? ORDER BY position LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        )

    async def finish(
        self,
        job_id: str,
        status: str,
        *,
        resolver: str | None,
        error_code: str | None = None,
        error_message: str | None = None,
        ttl: int,
    ) -> None:
        now = self._now()
        await self._execute(
            """
            UPDATE jobs SET status = ?, resolver = COALESCE(?, resolver), error_code = ?,
                error_message = ?, finished_at = ?, updated_at = ?, expires_at = ?,
                lease_owner = NULL, lease_until = NULL
            WHERE job_id = ?
            """,
            (status, resolver, error_code, error_message, now, now, now + ttl, job_id),
        )

    async def prune(self) -> int:
        now = self._now()
        async with self._lock:
            db = self._require()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    "DELETE FROM job_tracks WHERE job_id IN "
                    "(SELECT job_id FROM jobs WHERE expires_at <= ?)",
                    (now,),
                )
                cursor = await db.execute("DELETE FROM jobs WHERE expires_at <= ?", (now,))
                removed = cursor.rowcount
                await cursor.close()
                await db.execute("DELETE FROM playlist_cache WHERE expires_at <= ?", (now,))
                await db.execute("COMMIT")
            except BaseException:
                await db.execute("ROLLBACK")
                raise
        return removed

    # -------------------------------------------------------- playlist cache

    async def get_playlist(
        self, source: str, playlist_id: str, max_tracks: int
    ) -> PlaylistExtraction | None:
        row = await self._fetchone(
            "SELECT * FROM playlist_cache WHERE source = ? AND playlist_id = ? AND expires_at > ?",
            (source, playlist_id, self._now()),
        )
        if row is None or int(row["max_tracks"]) < max_tracks and row["truncated"]:
            return None
        try:
            entries = tuple(PlaylistEntry.from_dict(item) for item in json.loads(row["entries_json"]))
        except (ValueError, TypeError, KeyError):
            return None
        truncated = bool(row["truncated"]) or len(entries) > max_tracks
        entries = entries[:max_tracks]
        return PlaylistExtraction(
            source=source,
            playlist_id=playlist_id,
            title=row["title"],
            description=row["description"],
            owner=row["owner"],
            entries=entries,
            reported_total=row["reported_total"],
            extraction_complete=bool(row["extraction_complete"]) and not truncated,
            extractor=row["extractor"],
            truncated=truncated,
            incomplete_reason=row["incomplete_reason"] if not truncated else "max_tracks",
            stats=ExtractionStats(),
        )

    async def put_playlist(
        self, extraction: PlaylistExtraction, max_tracks: int, *, complete_ttl: int, partial_ttl: int
    ) -> None:
        now = self._now()
        ttl = complete_ttl if extraction.extraction_complete else partial_ttl
        await self._execute(
            """
            INSERT INTO playlist_cache (source, playlist_id, max_tracks, title, description, owner,
                reported_total, extraction_complete, truncated, incomplete_reason, extractor,
                entries_json, fetched_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, playlist_id) DO UPDATE SET
                max_tracks = excluded.max_tracks, title = excluded.title,
                description = excluded.description, owner = excluded.owner,
                reported_total = excluded.reported_total,
                extraction_complete = excluded.extraction_complete,
                truncated = excluded.truncated, incomplete_reason = excluded.incomplete_reason,
                extractor = excluded.extractor, entries_json = excluded.entries_json,
                fetched_at = excluded.fetched_at, expires_at = excluded.expires_at
            """,
            (
                extraction.source,
                extraction.playlist_id,
                max_tracks,
                extraction.title,
                extraction.description,
                extraction.owner,
                extraction.reported_total,
                int(extraction.extraction_complete),
                int(extraction.truncated),
                extraction.incomplete_reason,
                extraction.extractor,
                json.dumps([entry.to_dict() for entry in extraction.entries]),
                now,
                now + ttl,
            ),
        )


class TrackMatcher:
    """Match Spotify playlist entries via ytmusicapi with the shared cache."""

    def __init__(
        self,
        matcher: YTMusicMatcher,
        cache: Cache,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._matcher = matcher
        self._cache = cache
        self._settings = settings
        self._clock = clock
        self._sleep = sleep

    async def resolve(self, entry: PlaylistEntry) -> TrackResult:
        track = SpotifyTrack(
            spotify_id=entry.source_id or "",
            title=entry.title or "",
            artists=entry.artists,
            album=entry.album,
            duration_ms=entry.duration_ms,
            duration_precision="ms" if entry.duration_ms else None,
        )
        cached = await self._cached(track)
        if cached is not None:
            return cached
        outcome: MatchOutcome | None = None
        for attempt in range(_MAX_TRACK_RETRIES + 1):
            outcome = await self._matcher.match(track)
            if outcome.status != "failed":
                break
            if attempt < _MAX_TRACK_RETRIES:
                delay = self._settings.yt_music_retry_backoff_seconds * (2**attempt)
                if isinstance(outcome.error, ProviderRateLimited):
                    delay *= 4
                await self._sleep(delay)
        assert outcome is not None
        await self._store(track, outcome)
        return _result_from_outcome(outcome)

    async def _cached(self, track: SpotifyTrack) -> TrackResult | None:
        if not self._settings.resolver_cache_enabled:
            return None
        try:
            cached = await self._cache.get_resolution(track.spotify_id, "youtube", float(self._clock()))
        except Exception:
            logger.warning("Resolution cache read failed", exc_info=True)
            return None
        if cached is None:
            return None
        if cached.status == "matched" and cached.candidate is not None:
            # Re-validate against the current track metadata and threshold.
            rescored = score_candidate(track, cached.candidate.candidate, "youtube", self._matcher.scoring)
            if rescored.eligible and rescored.score.total >= self._matcher.scoring.threshold:
                return _matched(rescored, detail="cached")
            return None
        if cached.status == "not_found" and cached.provider == YTMUSIC:
            return TrackResult("unmatched", confidence=cached.best_confidence, provider=YTMUSIC, detail="cached")
        return None

    async def _store(self, track: SpotifyTrack, outcome: MatchOutcome) -> None:
        if not self._settings.resolver_cache_enabled:
            return
        try:
            if outcome.status == "matched" and outcome.match is not None:
                await self._cache.put_resolution(
                    track,
                    "youtube",
                    "matched",
                    candidate=outcome.match,
                    best_confidence=outcome.match.score.total,
                    now=float(self._clock()),
                    ttl_seconds=self._settings.match_cache_ttl_seconds,
                    provider=YTMUSIC,
                )
            elif outcome.status == "unmatched" and outcome.error is None:
                # Only a completed search is cached as negative; throttling and
                # timeouts are never cached as "no result".
                existing = await self._cache.get_resolution(
                    track.spotify_id, "youtube", float(self._clock())
                )
                if existing is None:
                    await self._cache.put_resolution(
                        track,
                        "youtube",
                        "not_found",
                        candidate=None,
                        best_confidence=outcome.best_confidence,
                        now=float(self._clock()),
                        ttl_seconds=self._settings.negative_cache_ttl_seconds,
                        provider=YTMUSIC,
                    )
        except Exception:
            logger.warning("Resolution cache write failed", exc_info=True)


def _matched(scored: ScoredCandidate, *, detail: str | None = None) -> TrackResult:
    candidate = scored.candidate
    return TrackResult(
        status="matched",
        youtube_id=_youtube_id(candidate.url),
        youtube_url=candidate.url,
        confidence=round(scored.score.total, 4),
        provider=candidate.source or YTMUSIC,
        match_title=candidate.title,
        match_artist=candidate.artist,
        detail=detail,
    )


def _result_from_outcome(outcome: MatchOutcome) -> TrackResult:
    if outcome.status == "matched" and outcome.match is not None:
        return _matched(outcome.match)
    best = outcome.best
    if outcome.status == "uncertain" and best is not None:
        return TrackResult(
            status="uncertain",
            youtube_id=_youtube_id(best.candidate.url),
            youtube_url=best.candidate.url,
            confidence=round(best.score.total, 4),
            provider=YTMUSIC,
            match_title=best.candidate.title,
            match_artist=best.candidate.artist,
            detail=best.rejection_reason or "below_threshold",
        )
    if outcome.status == "failed":
        return TrackResult(
            "failed",
            provider=YTMUSIC,
            detail=outcome.error.status if outcome.error is not None else "provider_error",
        )
    return TrackResult(
        "unmatched",
        confidence=round(outcome.best_confidence, 4) if outcome.best_confidence is not None else None,
        provider=YTMUSIC,
        detail="no_acceptable_candidate",
    )


class JobManager:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        *,
        spotify: SpotifyPlaylistService,
        youtube: YouTubePlaylistExtractor,
        track_matcher: TrackMatcher | None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._spotify = spotify
        self._youtube = youtube
        self._track_matcher = track_matcher
        self._owner = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._stopping = False

    @property
    def store(self) -> JobStore:
        return self._store

    @property
    def matching_available(self) -> bool:
        return self._track_matcher is not None

    async def submit(self, classified: ClassifiedInput, input_url: str) -> tuple[aiosqlite.Row, bool]:
        settings = self._settings
        max_tracks = settings.playlist_max_tracks
        existing = await self._store.find_reusable(
            classified.source,
            classified.identifier,
            max_tracks,
            complete_ttl=settings.playlist_cache_ttl,
            partial_ttl=settings.playlist_partial_cache_ttl,
        )
        if existing is not None:
            return existing, True
        if await self._store.count_active() >= settings.playlist_max_pending_jobs:
            raise QueueFull()
        job_id = new_job_id()
        await self._store.create(job_id, classified, input_url, max_tracks, settings.job_ttl_seconds)
        self._wake.set()
        row = await self._store.get(job_id)
        assert row is not None
        return row, False

    async def start(self) -> None:
        self._stopping = False
        for index in range(self._settings.playlist_max_concurrent_jobs):
            self._tasks.append(asyncio.create_task(self._worker(index), name=f"playlist-worker-{index}"))
        self._tasks.append(asyncio.create_task(self._janitor(), name="playlist-janitor"))

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            await self._store.release_owner(self._owner)
        except Exception:
            logger.warning("Could not release job leases on shutdown", exc_info=True)

    async def run_once(self) -> bool:
        """Claim and process one job. Returns False when the queue is empty."""
        job = await self._store.claim(self._owner, self._settings.job_lease_seconds)
        if job is None:
            return False
        await self._process(job)
        return True

    async def _worker(self, index: int) -> None:
        while not self._stopping:
            try:
                if await self.run_once():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Playlist worker %d crashed while claiming a job", index)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except TimeoutError:
                pass

    async def _janitor(self) -> None:
        while not self._stopping:
            try:
                removed = await self._store.prune()
                if removed:
                    logger.info("Pruned %d expired playlist jobs", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Job pruning failed", exc_info=True)
            await asyncio.sleep(600)

    async def _heartbeat(self, job_id: str) -> None:
        interval = max(1.0, self._settings.job_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await self._store.renew(job_id, self._owner, self._settings.job_lease_seconds)

    async def _process(self, job: aiosqlite.Row) -> None:
        job_id = job["job_id"]
        ttl = self._settings.job_ttl_seconds
        if job["attempts"] > _MAX_JOB_ATTEMPTS:
            await self._store.finish(
                job_id,
                "failed",
                resolver=None,
                error_code="job_interrupted",
                error_message="The job was interrupted repeatedly and was abandoned.",
                ttl=ttl,
            )
            return
        heartbeat = asyncio.create_task(self._heartbeat(job_id))
        try:
            if job["status"] in ("queued", "extracting"):
                await self._store.set_status(job_id, "extracting")
                extraction = await self._extract(job)
                await self._store.save_extraction(job_id, extraction, self._initial_rows(extraction))
            resolver = "yt-dlp" if job["source"] == "youtube" else YTMUSIC
            failed_matching = 0
            if job["source"] == "spotify":
                failed_matching = await self._match_pending(job_id)
            await self._finalize(job_id, resolver, failed_matching)
        except PlaylistError as exc:
            logger.info("Playlist job %s failed: %s", job_id, exc.code)
            await self._store.finish(
                job_id, "failed", resolver=None, error_code=exc.code, error_message=exc.message[:500], ttl=ttl
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Playlist job %s crashed", job_id)
            await self._store.finish(
                job_id,
                "failed",
                resolver=None,
                error_code="internal_error",
                error_message="An unexpected error occurred while processing the playlist.",
                ttl=ttl,
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _extract(self, job: aiosqlite.Row) -> PlaylistExtraction:
        source, playlist_id = job["source"], job["playlist_id"]
        max_tracks = int(job["max_tracks"])
        cached = await self._store.get_playlist(source, playlist_id, max_tracks)
        if cached is not None:
            return cached
        if source == "youtube":
            extraction = await self._youtube.extract(playlist_id, max_tracks)
        else:
            extraction = await self._spotify.extract(playlist_id, max_tracks)
        await self._store.put_playlist(
            extraction,
            max_tracks,
            complete_ttl=self._settings.playlist_cache_ttl,
            partial_ttl=self._settings.playlist_partial_cache_ttl,
        )
        return extraction

    def _initial_rows(self, extraction: PlaylistExtraction) -> list[tuple[PlaylistEntry, TrackResult]]:
        rows: list[tuple[PlaylistEntry, TrackResult]] = []
        for entry in extraction.entries:
            if not entry.available:
                rows.append((entry, TrackResult("unavailable", detail=entry.unavailable_reason)))
            elif extraction.source == "youtube":
                rows.append(
                    (
                        entry,
                        TrackResult(
                            "matched",
                            youtube_id=entry.source_id,
                            youtube_url=entry.url,
                            provider="yt-dlp",
                        ),
                    )
                )
            elif self._track_matcher is None:
                rows.append((entry, TrackResult("failed", detail="matching_disabled")))
            else:
                rows.append((entry, TrackResult("pending")))
        return rows

    async def _match_pending(self, job_id: str) -> int:
        matcher = self._track_matcher
        if matcher is None:
            return 0
        pending = await self._store.pending_tracks(job_id)
        if not pending:
            return 0
        queue: asyncio.Queue[aiosqlite.Row] = asyncio.Queue()
        for row in pending:
            queue.put_nowait(row)
        memo: dict[str, asyncio.Future[TrackResult]] = {}
        consecutive_failures = 0
        aborted = False

        async def resolve_entry(row: aiosqlite.Row) -> TrackResult:
            nonlocal consecutive_failures
            spotify_id = row["source_id"]
            future = memo.get(spotify_id)
            if future is not None:
                return await asyncio.shield(future)
            future = asyncio.get_running_loop().create_future()
            memo[spotify_id] = future
            try:
                result = await matcher.resolve(_entry_from_row(row))
            except Exception as exc:
                logger.warning("Track %s failed to resolve: %s", spotify_id, type(exc).__name__)
                result = TrackResult("failed", provider=YTMUSIC, detail="internal_error")
            future.set_result(result)
            consecutive_failures = consecutive_failures + 1 if result.status == "failed" else 0
            return result

        async def worker() -> None:
            nonlocal aborted
            while True:
                try:
                    row = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if aborted:
                    result = TrackResult("failed", provider=YTMUSIC, detail="provider_unavailable")
                else:
                    result = await resolve_entry(row)
                    if consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
                        aborted = True
                await self._store.update_track(job_id, row["position"], result)

        concurrency = max(1, self._settings.yt_music_concurrency)
        await asyncio.gather(*(worker() for _ in range(concurrency)))
        if aborted:
            logger.warning("Job %s stopped matching after repeated provider failures", job_id)
        counts = await self._store.counts(job_id)
        return counts["failed"]

    async def _finalize(self, job_id: str, resolver: str, failed: int) -> None:
        row = await self._store.get(job_id)
        assert row is not None
        counts = await self._store.counts(job_id)
        complete = bool(row["extraction_complete"])
        status = "completed" if complete and counts["failed"] == 0 and counts["pending"] == 0 else "partial"
        await self._store.finish(job_id, status, resolver=resolver, ttl=self._settings.job_ttl_seconds)


def _entry_from_row(row: aiosqlite.Row) -> PlaylistEntry:
    return PlaylistEntry(
        position=int(row["position"]),
        source_id=row["source_id"],
        title=row["title"],
        artists=tuple(json.loads(row["artists_json"] or "[]")),
        duration_ms=row["duration_ms"],
        album=row["album"],
        isrc=row["isrc"],
        url=row["source_url"],
    )
