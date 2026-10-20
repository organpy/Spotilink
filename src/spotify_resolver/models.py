from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PLATFORMS = ("youtube", "soundcloud")


@dataclass(frozen=True, slots=True)
class SpotifyTrack:
    spotify_id: str
    title: str
    artists: tuple[str, ...]
    album: str | None = None
    duration_ms: int | None = None
    duration_precision: str | None = None

    @property
    def url(self) -> str:
        return f"https://open.spotify.com/track/{self.spotify_id}"


@dataclass(frozen=True, slots=True)
class Candidate:
    platform: str
    url: str
    title: str
    artist: str | None = None
    uploader: str | None = None
    duration_ms: int | None = None
    channel_id: str | None = None
    is_topic: bool = False
    is_vevo: bool = False
    official_hint: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    title: float
    artist: float
    duration: float | None
    official_bonus: float
    version_penalty: float
    total: float
    capped: bool = False
    version_tags: tuple[str, ...] = ()
    duration_diff_ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": round(self.title, 4),
            "artist": round(self.artist, 4),
            "duration": None if self.duration is None else round(self.duration, 4),
            "official_bonus": round(self.official_bonus, 4),
            "version_penalty": round(self.version_penalty, 4),
            "total": round(self.total, 4),
            "capped": self.capped,
            "version_tags": list(self.version_tags),
            "duration_diff_ms": self.duration_diff_ms,
        }


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: Candidate
    score: ScoreBreakdown
    eligible: bool
    rejection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Attempt:
    platform: str
    status: str
    cached: bool = False
    best_confidence: float | None = None


@dataclass(frozen=True, slots=True)
class SearchFallback:
    platform: str
    url: str
    verified: bool = False
    reason: str = "no_verified_match"


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    track: SpotifyTrack
    match: ScoredCandidate | None
    search_fallback: SearchFallback | None
    cached: bool
    metadata_cached: bool
    attempts: tuple[Attempt, ...] = ()


@dataclass(frozen=True, slots=True)
class CachedResolution:
    spotify_id: str
    platform: str
    status: str
    track: SpotifyTrack
    url: str | None
    confidence: float | None
    best_confidence: float | None
    candidate: ScoredCandidate | None
    resolved_at: float
    expires_at: float


def candidate_to_cache_dict(scored: ScoredCandidate) -> dict[str, Any]:
    candidate = scored.candidate
    return {
        "candidate": {
            "platform": candidate.platform,
            "url": candidate.url,
            "title": candidate.title,
            "artist": candidate.artist,
            "uploader": candidate.uploader,
            "duration_ms": candidate.duration_ms,
            "channel_id": candidate.channel_id,
            "is_topic": candidate.is_topic,
            "is_vevo": candidate.is_vevo,
            "official_hint": candidate.official_hint,
        },
        "score": scored.score.as_dict(),
        "eligible": scored.eligible,
        "rejection_reason": scored.rejection_reason,
    }


def scored_candidate_from_cache_dict(data: dict[str, Any]) -> ScoredCandidate:
    candidate_data = data["candidate"]
    score_data = data["score"]
    candidate = Candidate(
        platform=str(candidate_data["platform"]),
        url=str(candidate_data["url"]),
        title=str(candidate_data["title"]),
        artist=_optional_str(candidate_data.get("artist")),
        uploader=_optional_str(candidate_data.get("uploader")),
        duration_ms=_optional_int(candidate_data.get("duration_ms")),
        channel_id=_optional_str(candidate_data.get("channel_id")),
        is_topic=bool(candidate_data.get("is_topic", False)),
        is_vevo=bool(candidate_data.get("is_vevo", False)),
        official_hint=bool(candidate_data.get("official_hint", False)),
    )
    score = ScoreBreakdown(
        title=float(score_data["title"]),
        artist=float(score_data["artist"]),
        duration=_optional_float(score_data.get("duration")),
        official_bonus=float(score_data["official_bonus"]),
        version_penalty=float(score_data["version_penalty"]),
        total=float(score_data["total"]),
        capped=bool(score_data.get("capped", False)),
        version_tags=tuple(str(x) for x in score_data.get("version_tags", [])),
        duration_diff_ms=_optional_int(score_data.get("duration_diff_ms")),
    )
    return ScoredCandidate(
        candidate=candidate,
        score=score,
        eligible=bool(data.get("eligible", True)),
        rejection_reason=_optional_str(data.get("rejection_reason")),
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
