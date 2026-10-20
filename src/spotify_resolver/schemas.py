from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    Attempt,
    ResolutionResult,
    ScoreBreakdown,
    ScoredCandidate,
    SearchFallback,
    SpotifyTrack,
)


class ScoreSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: float
    artist: float
    duration: float | None = None
    official_bonus: float
    version_penalty: float
    total: float
    capped: bool
    version_tags: list[str] = Field(default_factory=list)
    duration_diff_ms: int | None = None


class MatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    url: str
    title: str
    artist: str | None = None
    uploader: str | None = None
    duration_ms: int | None = None
    confidence: float
    score: ScoreSchema


class SearchFallbackSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    url: str
    verified: bool = False
    reason: str = "no_verified_match"


class AttemptSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    status: str
    cached: bool = False
    best_confidence: float | None = None


class ErrorSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: float | None = None


class ResolveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    spotify_id: str | None = None
    title: str | None = None
    artists: list[str] = Field(default_factory=list)
    album: str | None = None
    duration_ms: int | None = None
    duration_precision: str | None = None
    spotify_url: str | None = None
    match: MatchSchema | None = None
    search_fallback: SearchFallbackSchema | None = None
    cached: bool = False
    metadata_cached: bool = False
    attempts: list[AttemptSchema] = Field(default_factory=list)
    error: ErrorSchema | None = None


def response_from_result(result: ResolutionResult) -> ResolveResponse:
    track = result.track
    return ResolveResponse(
        ok=True,
        **_track_fields(track),
        match=_match_schema(result.match) if result.match is not None else None,
        search_fallback=(
            _fallback_schema(result.search_fallback)
            if result.search_fallback is not None
            else None
        ),
        cached=result.cached,
        metadata_cached=result.metadata_cached,
        attempts=[_attempt_schema(attempt) for attempt in result.attempts],
        error=None,
    )


def response_from_error(exc: Any) -> ResolveResponse:
    track = getattr(exc, "track", None)
    attempts = getattr(exc, "attempts", ()) or ()
    fallback = getattr(exc, "search_fallback", None)
    retry_after = getattr(exc, "retry_after_seconds", None)
    return ResolveResponse(
        ok=False,
        **(_track_fields(track) if track is not None else {}),
        match=None,
        search_fallback=_fallback_schema(fallback) if fallback is not None else None,
        cached=False,
        metadata_cached=bool(getattr(exc, "metadata_cached", False)),
        attempts=[_attempt_schema(attempt) for attempt in attempts],
        error=ErrorSchema(
            code=str(getattr(exc, "code", "internal_error")),
            message=str(getattr(exc, "message", "An unexpected error occurred.")),
            retryable=bool(getattr(exc, "retryable", False)),
            retry_after_seconds=float(retry_after) if retry_after is not None else None,
        ),
    )


def _track_fields(track: SpotifyTrack) -> dict[str, Any]:
    return {
        "spotify_id": track.spotify_id,
        "title": track.title,
        "artists": list(track.artists),
        "album": track.album,
        "duration_ms": track.duration_ms,
        "duration_precision": track.duration_precision,
        "spotify_url": track.url,
    }


def _match_schema(scored: ScoredCandidate) -> MatchSchema:
    candidate = scored.candidate
    return MatchSchema(
        platform=candidate.platform,
        url=candidate.url,
        title=candidate.title,
        artist=candidate.artist,
        uploader=candidate.uploader,
        duration_ms=candidate.duration_ms,
        confidence=round(scored.score.total, 4),
        score=_score_schema(scored.score),
    )


def _score_schema(score: ScoreBreakdown) -> ScoreSchema:
    return ScoreSchema(
        title=round(score.title, 4),
        artist=round(score.artist, 4),
        duration=None if score.duration is None else round(score.duration, 4),
        official_bonus=round(score.official_bonus, 4),
        version_penalty=round(score.version_penalty, 4),
        total=round(score.total, 4),
        capped=score.capped,
        version_tags=list(score.version_tags),
        duration_diff_ms=score.duration_diff_ms,
    )


def _fallback_schema(fallback: SearchFallback) -> SearchFallbackSchema:
    return SearchFallbackSchema(
        platform=fallback.platform,
        url=fallback.url,
        verified=fallback.verified,
        reason=fallback.reason,
    )


def _attempt_schema(attempt: Attempt) -> AttemptSchema:
    return AttemptSchema(
        platform=attempt.platform,
        status=attempt.status,
        cached=attempt.cached,
        best_confidence=(
            round(attempt.best_confidence, 4)
            if attempt.best_confidence is not None
            else None
        ),
    )
