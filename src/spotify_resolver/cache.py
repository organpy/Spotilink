from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    CachedResolution,
    ScoredCandidate,
    SpotifyTrack,
    candidate_to_cache_dict,
    scored_candidate_from_cache_dict,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spotify_metadata (
    spotify_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('ok', 'not_found')),
    title TEXT,
    artists_json TEXT,
    album TEXT,
    duration_ms INTEGER,
    duration_precision TEXT,
    fetched_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resolutions (
    spotify_id TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('youtube', 'soundcloud')),
    status TEXT NOT NULL CHECK (status IN ('matched', 'not_found')),
    url TEXT,
    confidence REAL,
    best_confidence REAL,
    candidate_json TEXT,
    title TEXT NOT NULL,
    artists_json TEXT NOT NULL,
    album TEXT,
    duration_ms INTEGER,
    duration_precision TEXT,
    resolved_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (spotify_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_spotify_metadata_expiry
    ON spotify_metadata (expires_at);
CREATE INDEX IF NOT EXISTS idx_resolutions_expiry
    ON resolutions (expires_at);
"""


SCHEMA_VERSION = 2


async def _migrate(db: aiosqlite.Connection) -> None:
    """Additive, idempotent migrations. Existing rows are preserved."""
    cursor = await db.execute("PRAGMA table_info(resolutions)")
    columns = {row[1] for row in await cursor.fetchall()}
    await cursor.close()
    if "provider" not in columns:
        # v2: which backend produced the resolution (youtube_data_api,
        # ytmusicapi, soundcloud_api). NULL for rows written before v2.
        await db.execute("ALTER TABLE resolutions ADD COLUMN provider TEXT")
    await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


class Cache:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        if self._db is not None:
            return
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(self._path))
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.executescript(_SCHEMA)
            await _migrate(db)
            await db.commit()
        except Exception:
            await db.close()
            raise
        self._db = db
        if str(self._path) != ":memory:":
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def ping(self) -> bool:
        try:
            db = self._require_db()
            async with self._lock:
                cursor = await db.execute("SELECT 1")
                row = await cursor.fetchone()
                await cursor.close()
            return bool(row and row[0] == 1)
        except (RuntimeError, sqlite3.Error, ValueError):
            return False

    async def get_metadata(
        self,
        spotify_id: str,
        now: float,
    ) -> tuple[str, SpotifyTrack | None] | None:
        db = self._require_db()
        async with self._lock:
            cursor = await db.execute(
                "SELECT * FROM spotify_metadata WHERE spotify_id = ? AND expires_at > ?",
                (spotify_id, int(now)),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        status = row["status"]
        if status == "not_found":
            return ("not_found", None)
        if status != "ok":
            return None
        track = _track_from_row(row, spotify_id)
        if track is None:
            return None
        return ("ok", track)

    async def put_metadata(
        self,
        track: SpotifyTrack,
        now: float,
        ttl_seconds: int,
    ) -> None:
        db = self._require_db()
        async with self._lock:
            await db.execute(
                """
                INSERT INTO spotify_metadata (
                    spotify_id, status, title, artists_json, album,
                    duration_ms, duration_precision, fetched_at, expires_at
                ) VALUES (?, 'ok', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(spotify_id) DO UPDATE SET
                    status = 'ok',
                    title = excluded.title,
                    artists_json = excluded.artists_json,
                    album = excluded.album,
                    duration_ms = excluded.duration_ms,
                    duration_precision = excluded.duration_precision,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (
                    track.spotify_id,
                    track.title,
                    json.dumps(list(track.artists)),
                    track.album,
                    track.duration_ms,
                    track.duration_precision,
                    int(now),
                    int(now) + ttl_seconds,
                ),
            )
            await db.commit()

    async def put_metadata_not_found(
        self,
        spotify_id: str,
        now: float,
        ttl_seconds: int,
    ) -> None:
        db = self._require_db()
        async with self._lock:
            await db.execute(
                """
                INSERT INTO spotify_metadata (
                    spotify_id, status, title, artists_json, album,
                    duration_ms, duration_precision, fetched_at, expires_at
                ) VALUES (?, 'not_found', NULL, NULL, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(spotify_id) DO UPDATE SET
                    status = 'not_found',
                    title = NULL,
                    artists_json = NULL,
                    album = NULL,
                    duration_ms = NULL,
                    duration_precision = NULL,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (spotify_id, int(now), int(now) + ttl_seconds),
            )
            await db.execute(
                "DELETE FROM resolutions WHERE spotify_id = ?",
                (spotify_id,),
            )
            await db.commit()

    async def get_resolution(
        self,
        spotify_id: str,
        platform: str,
        now: float,
    ) -> CachedResolution | None:
        db = self._require_db()
        async with self._lock:
            cursor = await db.execute(
                """
                SELECT * FROM resolutions
                WHERE spotify_id = ? AND platform = ? AND expires_at > ?
                """,
                (spotify_id, platform, int(now)),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None

        return _resolution_from_row(row, spotify_id, platform)

    async def get_stale_resolution(
        self,
        spotify_id: str,
        platform: str,
        now: float,
        grace_seconds: int,
    ) -> CachedResolution | None:
        if grace_seconds <= 0:
            return None
        db = self._require_db()
        async with self._lock:
            cursor = await db.execute(
                """
                SELECT * FROM resolutions
                WHERE spotify_id = ? AND platform = ? AND status = 'matched'
                  AND expires_at <= ? AND expires_at > ?
                """,
                (
                    spotify_id,
                    platform,
                    int(now),
                    int(now) - int(grace_seconds),
                ),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            return None
        return _resolution_from_row(row, spotify_id, platform)

    async def put_resolution(
        self,
        track: SpotifyTrack,
        platform: str,
        status: str,
        *,
        candidate: ScoredCandidate | None,
        best_confidence: float | None,
        now: float,
        ttl_seconds: int,
        provider: str | None = None,
    ) -> None:
        if status not in {"matched", "not_found"}:
            raise ValueError("Invalid resolution status")
        if status == "matched" and candidate is None:
            raise ValueError("A matched resolution requires a candidate")
        if candidate is not None and candidate.candidate.platform != platform:
            raise ValueError("Candidate platform does not match resolution platform")

        db = self._require_db()
        url = candidate.candidate.url if candidate else None
        confidence = candidate.score.total if candidate else None
        candidate_json = (
            json.dumps(candidate_to_cache_dict(candidate)) if candidate else None
        )
        if provider is None and candidate is not None:
            provider = candidate.candidate.source
        async with self._lock:
            await db.execute(
                """
                INSERT INTO resolutions (
                    spotify_id, platform, status, url, confidence, best_confidence,
                    candidate_json, title, artists_json, album, duration_ms,
                    duration_precision, resolved_at, expires_at, provider
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(spotify_id, platform) DO UPDATE SET
                    status = excluded.status,
                    url = excluded.url,
                    confidence = excluded.confidence,
                    best_confidence = excluded.best_confidence,
                    candidate_json = excluded.candidate_json,
                    title = excluded.title,
                    artists_json = excluded.artists_json,
                    album = excluded.album,
                    duration_ms = excluded.duration_ms,
                    duration_precision = excluded.duration_precision,
                    resolved_at = excluded.resolved_at,
                    expires_at = excluded.expires_at,
                    provider = excluded.provider
                """,
                (
                    track.spotify_id,
                    platform,
                    status,
                    url,
                    confidence,
                    best_confidence,
                    candidate_json,
                    track.title,
                    json.dumps(list(track.artists)),
                    track.album,
                    track.duration_ms,
                    track.duration_precision,
                    int(now),
                    int(now) + ttl_seconds,
                    provider,
                ),
            )
            await db.commit()

    async def prune(
        self,
        now: float,
        *,
        stale_match_grace_seconds: int = 0,
    ) -> None:
        db = self._require_db()
        retain_after = int(now) - max(0, int(stale_match_grace_seconds))
        async with self._lock:
            await db.execute(
                "DELETE FROM spotify_metadata WHERE expires_at <= ?",
                (int(now),),
            )
            await db.execute(
                """
                DELETE FROM resolutions
                WHERE expires_at <= ? AND (status = 'not_found' OR expires_at <= ?)
                """,
                (int(now), retain_after),
            )
            await db.commit()

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Cache has not been initialized")
        return self._db


def _resolution_from_row(
    row: aiosqlite.Row,
    spotify_id: str,
    platform: str,
) -> CachedResolution | None:
    track = _track_from_row(row, spotify_id)
    if track is None:
        return None
    status = row["status"]
    if status == "not_found":
        return CachedResolution(
            provider=_row_provider(row),
            spotify_id=spotify_id,
            platform=platform,
            status="not_found",
            track=track,
            url=None,
            confidence=None,
            best_confidence=_float_or_none(row["best_confidence"]),
            candidate=None,
            resolved_at=float(row["resolved_at"]),
            expires_at=float(row["expires_at"]),
        )
    if status != "matched":
        return None

    candidate_json = row["candidate_json"]
    if not candidate_json:
        return None
    try:
        candidate = scored_candidate_from_cache_dict(json.loads(candidate_json))
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
    if (
        candidate.candidate.platform != platform
        or candidate.candidate.url != row["url"]
    ):
        return None
    return CachedResolution(
        provider=_row_provider(row),
        spotify_id=spotify_id,
        platform=platform,
        status="matched",
        track=track,
        url=row["url"],
        confidence=_float_or_none(row["confidence"]),
        best_confidence=_float_or_none(row["best_confidence"]),
        candidate=candidate,
        resolved_at=float(row["resolved_at"]),
        expires_at=float(row["expires_at"]),
    )


def _track_from_row(row: aiosqlite.Row, spotify_id: str) -> SpotifyTrack | None:
    title = row["title"]
    artists_json = row["artists_json"]
    if not title or not artists_json:
        return None
    try:
        artists = json.loads(artists_json)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(artists, list):
        return None
    artist_values = tuple(str(artist) for artist in artists if str(artist).strip())
    if not artist_values:
        return None
    duration = row["duration_ms"]
    return SpotifyTrack(
        spotify_id=spotify_id,
        title=str(title),
        artists=artist_values,
        album=row["album"],
        duration_ms=int(duration) if duration is not None else None,
        duration_precision=row["duration_precision"],
    )


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def _row_provider(row: aiosqlite.Row) -> str | None:
    try:
        value = row["provider"]
    except (IndexError, KeyError):
        return None
    return None if value is None else str(value)
