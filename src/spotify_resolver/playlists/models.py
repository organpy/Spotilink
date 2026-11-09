from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PlaylistEntry:
    """One item of a source playlist, in its original 1-based position.

    `available` is False for items the source lists but cannot provide
    (removed tracks, local files, episodes, private/deleted videos). They are
    kept so that positions and counts stay faithful to the source.
    """

    position: int
    source_id: str | None
    title: str | None
    artists: tuple[str, ...] = ()
    duration_ms: int | None = None
    album: str | None = None
    isrc: str | None = None
    url: str | None = None
    available: bool = True
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "source_id": self.source_id,
            "title": self.title,
            "artists": list(self.artists),
            "duration_ms": self.duration_ms,
            "album": self.album,
            "isrc": self.isrc,
            "url": self.url,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaylistEntry:
        return cls(
            position=int(data["position"]),
            source_id=data.get("source_id"),
            title=data.get("title"),
            artists=tuple(str(a) for a in data.get("artists") or ()),
            duration_ms=(
                int(data["duration_ms"]) if data.get("duration_ms") is not None else None
            ),
            album=data.get("album"),
            isrc=data.get("isrc"),
            url=data.get("url"),
            available=bool(data.get("available", True)),
            unavailable_reason=data.get("unavailable_reason"),
        )


@dataclass(frozen=True, slots=True)
class ExtractionStats:
    requests: int = 0
    pages: int = 0
    elapsed_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class PlaylistExtraction:
    source: str
    playlist_id: str
    title: str | None
    entries: tuple[PlaylistEntry, ...]
    reported_total: int | None
    extraction_complete: bool
    extractor: str
    description: str | None = None
    owner: str | None = None
    truncated: bool = False
    incomplete_reason: str | None = None
    stats: ExtractionStats = field(default_factory=ExtractionStats)
    # Diagnostics from extractors that failed before this one succeeded.
    fallback_errors: tuple[str, ...] = ()

    @property
    def extracted_total(self) -> int:
        return len(self.entries)


class PlaylistError(Exception):
    """Base error for playlist extraction.

    `recoverable` tells the orchestrator whether another implementation may
    succeed. `blocked` marks upstream denial (rate limiting, 403) that would
    equally affect any implementation using the same endpoint.
    """

    code = "playlist_extraction_failed"
    status_code = 502

    def __init__(
        self,
        message: str,
        *,
        recoverable: bool = True,
        blocked: bool = False,
        retry_after_seconds: float | None = None,
        partial: PlaylistExtraction | None = None,
        requests: int = 0,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.recoverable = recoverable
        self.blocked = blocked
        self.retry_after_seconds = retry_after_seconds
        self.partial = partial
        self.requests = requests


class PlaylistNotFound(PlaylistError):
    code = "playlist_not_found"
    status_code = 404

    def __init__(self, message: str = "The playlist does not exist or is private.") -> None:
        super().__init__(message, recoverable=False)


class PlaylistEmpty(PlaylistError):
    code = "playlist_empty"
    status_code = 422

    def __init__(self, message: str = "The playlist has no tracks.") -> None:
        super().__init__(message, recoverable=False)


class PlaylistSchemaChanged(PlaylistError):
    code = "upstream_schema_changed"


class PlaylistUpstreamBlocked(PlaylistError):
    code = "upstream_blocked"
    status_code = 503

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("blocked", True)
        super().__init__(message, **kwargs)


class PlaylistUpstreamUnavailable(PlaylistError):
    code = "upstream_unavailable"
    status_code = 503
