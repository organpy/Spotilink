"""/v2 endpoints: track resolution, playlist jobs, health."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .api import client_key
from .errors import ResolverError
from .jobs import QueueFull
from .schemas import ResolveResponse, response_from_result
from .urls import InputKind, UnsupportedInput, classify_url

router = APIRouter(prefix="/v2")

MAX_PAGE_LIMIT = 500


class V2Error(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None


class V2ErrorResponse(BaseModel):
    ok: Literal[False] = False
    error: V2Error


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Spotify track/playlist URL or YouTube playlist URL")


class JobCreated(BaseModel):
    type: Literal["playlist"] = "playlist"
    source: str
    job_id: str
    status: str
    reused: bool = False
    status_url: str
    tracks_url: str


class TrackResolved(BaseModel):
    type: Literal["track"] = "track"
    source: Literal["spotify"] = "spotify"
    resolution: ResolveResponse


class PlaylistInfo(BaseModel):
    id: str
    title: str | None = None
    description: str | None = None
    owner: str | None = None
    reported_total: int | None = None
    extracted_total: int
    extraction_complete: bool | None = None
    incomplete_reason: str | None = None
    truncated: bool = False


class Progress(BaseModel):
    processed: int
    total: int
    matched: int
    uncertain: int
    unmatched: int
    unavailable: int
    failed: int
    pending: int


class JobStatus(BaseModel):
    job_id: str
    status: str
    source: str
    playlist: PlaylistInfo | None = None
    progress: Progress
    extractor: str | None = None
    resolver: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: V2Error | None = None
    created_at: int
    updated_at: int
    finished_at: int | None = None
    tracks_url: str


class TrackItem(BaseModel):
    position: int
    source_id: str | None = None
    spotify_id: str | None = None
    youtube_id: str | None = None
    title: str | None = None
    artists: list[str] = Field(default_factory=list)
    duration_ms: int | None = None
    album: str | None = None
    isrc: str | None = None
    youtube_url: str | None = None
    confidence: float | None = None
    provider: str | None = None
    status: str
    match_title: str | None = None
    match_artist: str | None = None
    detail: str | None = None


class TracksPage(BaseModel):
    job_id: str
    status: str
    offset: int
    limit: int
    total: int
    tracks: list[TrackItem]
    next_offset: int | None = None


def _error(
    code: str,
    message: str,
    status_code: int,
    *,
    retryable: bool = False,
    retry_after: float | None = None,
) -> JSONResponse:
    headers = {"Retry-After": str(max(1, math.ceil(retry_after)))} if retry_after else None
    body = V2ErrorResponse(
        error=V2Error(code=code, message=message, retryable=retryable, retry_after_seconds=retry_after)
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"), headers=headers)


def _job_urls(job_id: str) -> tuple[str, str]:
    return f"/v2/jobs/{job_id}", f"/v2/jobs/{job_id}/tracks"


@router.post(
    "/resolve",
    summary="Resolve a Spotify track, Spotify playlist or YouTube playlist",
    responses={
        200: {"model": TrackResolved},
        202: {"model": JobCreated},
        400: {"model": V2ErrorResponse},
        422: {"model": V2ErrorResponse},
        429: {"model": V2ErrorResponse},
        503: {"model": V2ErrorResponse},
    },
)
async def resolve(request: Request, body: ResolveRequest) -> Response:
    try:
        classified = classify_url(body.url)
    except UnsupportedInput as exc:
        return _error(exc.code, exc.message, 422)

    state = request.app.state
    settings = state.settings
    if classified.kind is InputKind.SPOTIFY_TRACK:
        service = state.service
        try:
            result = await asyncio.wait_for(
                service.resolve(classified.canonical_url, "youtube", True),
                timeout=settings.resolve_timeout_seconds,
            )
        except TimeoutError:
            return _error("resolution_timeout", "Resolution exceeded the configured timeout.", 504, retryable=True)
        except ResolverError as exc:
            return _error(
                exc.code,
                exc.message,
                exc.status_code,
                retryable=exc.retryable,
                retry_after=exc.retry_after_seconds,
            )
        payload = TrackResolved(resolution=response_from_result(result))
        return JSONResponse(status_code=200, content=payload.model_dump(mode="json"))

    jobs = getattr(state, "jobs", None)
    if jobs is None:
        return _error("playlists_disabled", "Playlist processing is not available.", 503)
    if classified.source == "spotify" and not jobs.matching_available:
        return _error(
            "matcher_unavailable",
            "Spotify playlist matching requires YT_MUSIC_ENABLED=true.",
            503,
        )
    try:
        row, reused = await jobs.submit(classified, body.url)
    except QueueFull:
        return _error(
            "queue_full",
            "Too many playlist jobs are queued. Try again shortly.",
            503,
            retryable=True,
            retry_after=30,
        )
    status_url, tracks_url = _job_urls(row["job_id"])
    payload = JobCreated(
        source=row["source"],
        job_id=row["job_id"],
        status=row["status"],
        reused=reused,
        status_url=status_url,
        tracks_url=tracks_url,
    )
    return JSONResponse(
        status_code=202,
        content=payload.model_dump(mode="json"),
        headers={"Location": status_url},
    )


@router.get("/jobs/{job_id}", response_model=JobStatus, responses={404: {"model": V2ErrorResponse}})
async def get_job(request: Request, job_id: str) -> Response:
    jobs = request.app.state.jobs
    row = await jobs.store.get(job_id) if _valid_job_id(job_id) else None
    if row is None:
        return _error("job_not_found", "No such job, or it has expired.", 404)
    counts = await jobs.store.counts(job_id)
    total = int(row["total"])
    pending = counts["pending"]
    progress = Progress(
        processed=total - pending if row["status"] not in ("queued", "extracting") else 0,
        total=total,
        matched=counts["matched"],
        uncertain=counts["uncertain"],
        unmatched=counts["unmatched"],
        unavailable=counts["unavailable"],
        failed=counts["failed"],
        pending=pending,
    )
    extracted = row["extraction_complete"] is not None
    playlist = PlaylistInfo(
        id=row["playlist_id"],
        title=row["title"],
        description=row["description"],
        owner=row["owner"],
        reported_total=row["reported_total"],
        extracted_total=total,
        extraction_complete=bool(row["extraction_complete"]) if extracted else None,
        incomplete_reason=row["incomplete_reason"],
        truncated=bool(row["truncated"]),
    )
    error = None
    if row["error_code"]:
        error = V2Error(code=row["error_code"], message=row["error_message"] or "", retryable=False)
    warnings = json.loads(row["warnings_json"]) if row["warnings_json"] else []
    _, tracks_url = _job_urls(job_id)
    body = JobStatus(
        job_id=job_id,
        status=row["status"],
        source=row["source"],
        playlist=playlist if extracted or row["title"] else None,
        progress=progress,
        extractor=row["extractor"],
        resolver=row["resolver"],
        warnings=warnings,
        error=error,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
        tracks_url=tracks_url,
    )
    return JSONResponse(content=body.model_dump(mode="json"))


@router.get("/jobs/{job_id}/tracks", response_model=TracksPage, responses={404: {"model": V2ErrorResponse}})
async def get_tracks(
    request: Request,
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_LIMIT),
) -> Response:
    jobs = request.app.state.jobs
    row = await jobs.store.get(job_id) if _valid_job_id(job_id) else None
    if row is None:
        return _error("job_not_found", "No such job, or it has expired.", 404)
    total = int(row["total"])
    rows = await jobs.store.tracks(job_id, offset, limit)
    tracks = [
        TrackItem(
            position=r["position"],
            source_id=r["source_id"],
            spotify_id=r["source_id"] if row["source"] == "spotify" else None,
            youtube_id=r["youtube_id"],
            title=r["title"],
            artists=json.loads(r["artists_json"] or "[]"),
            duration_ms=r["duration_ms"],
            album=r["album"],
            isrc=r["isrc"],
            youtube_url=r["youtube_url"],
            confidence=r["confidence"],
            provider=r["provider"],
            status=r["status"],
            match_title=r["match_title"],
            match_artist=r["match_artist"],
            detail=r["detail"],
        )
        for r in rows
    ]
    next_offset = offset + len(rows) if offset + len(rows) < total else None
    body = TracksPage(
        job_id=job_id,
        status=row["status"],
        offset=offset,
        limit=limit,
        total=total,
        tracks=tracks,
        next_offset=next_offset,
    )
    return JSONResponse(content=body.model_dump(mode="json"))


@router.get("/health", summary="Health and provider availability")
async def health(request: Request) -> JSONResponse:
    state = request.app.state
    settings = state.settings
    database_ok = await state.cache.ping()
    primary = state.providers.get("youtube")
    breaker = getattr(state, "primary_breaker", None)
    matcher = getattr(state, "ytmusic_matcher", None)
    jobs = getattr(state, "jobs", None)
    active = await jobs.store.count_active() if jobs is not None else 0
    body: dict[str, Any] = {
        "status": "ok" if database_ok else "degraded",
        "version": "2",
        "database": "ok" if database_ok else "unavailable",
        "providers": {
            "youtube_data_api": {
                "configured": bool(getattr(primary, "configured", False)),
                "circuit": breaker.snapshot() if breaker is not None else None,
            },
            "soundcloud": {
                "configured": bool(getattr(state.providers.get("soundcloud"), "configured", False))
            },
            "ytmusicapi": {"enabled": matcher is not None},
            "spotify_playlist_extractors": (
                state.spotify_playlists.extractor_names if getattr(state, "spotify_playlists", None) else []
            ),
            "yt_dlp": {"enabled": jobs is not None},
        },
        "jobs": {
            "active": active,
            "max_concurrent": settings.playlist_max_concurrent_jobs,
            "max_pending": settings.playlist_max_pending_jobs,
            "max_tracks": settings.playlist_max_tracks,
        },
    }
    return JSONResponse(status_code=200 if database_ok else 503, content=body)


def _valid_job_id(job_id: str) -> bool:
    return 4 < len(job_id) <= 64 and job_id.startswith("job_") and all(
        c.isalnum() or c in "-_" for c in job_id
    )


class V2RateLimitMiddleware(BaseHTTPMiddleware):
    """Limits POST /v2/resolve per client; polling endpoints are not limited."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method != "POST" or request.url.path != "/v2/resolve":
            return await call_next(request)
        limiter = getattr(request.app.state, "v2_limiter", None)
        if limiter is None or not limiter.enabled:
            return await call_next(request)
        key = client_key(request, trust_proxy=request.app.state.settings.trust_proxy_headers)
        retry_after = limiter.check(key)
        if retry_after is None:
            return await call_next(request)
        return _error(
            "rate_limited",
            "Too many resolution requests from this client.",
            429,
            retryable=True,
            retry_after=retry_after,
        )

