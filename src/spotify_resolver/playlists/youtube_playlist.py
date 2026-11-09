"""YouTube playlist extraction with yt-dlp (metadata only).

yt-dlp runs with flat playlist extraction (`extract_flat="in_playlist"`) and
`skip_download`, so it lists playlist entries without resolving or probing any
video stream and never downloads media. The URL handed to yt-dlp is always the
canonical `https://www.youtube.com/playlist?list=<validated id>`, never the
caller's input.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..urls import is_valid_video_id
from .models import (
    ExtractionStats,
    PlaylistEmpty,
    PlaylistEntry,
    PlaylistError,
    PlaylistExtraction,
    PlaylistNotFound,
    PlaylistUpstreamBlocked,
    PlaylistUpstreamUnavailable,
)
from .spotify_pathfinder import Stopwatch

logger = logging.getLogger(__name__)

EXTRACTOR_NAME = "yt-dlp"
_UNAVAILABLE_TITLES = {
    "[private video]": "private",
    "[deleted video]": "deleted",
    "[unavailable video]": "unavailable",
}
_UNAVAILABLE_AVAILABILITY = {
    "private": "private",
    "needs_auth": "requires_sign_in",
    "subscriber_only": "members_only",
    "premium_only": "premium_only",
}


class _QuietLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)


def ydl_options(max_tracks: int) -> dict[str, Any]:
    return {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "noplaylist": False,
        "playlistend": max_tracks,
        "lazy_playlist": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": False,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "check_formats": False,
        "logger": _QuietLogger(),
    }


def _default_runner(url: str, options: dict[str, Any]) -> dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


class YouTubePlaylistExtractor:
    name = EXTRACTOR_NAME

    def __init__(
        self,
        settings: Settings,
        *,
        runner: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner or _default_runner
        self._semaphore = semaphore or asyncio.Semaphore(
            max(1, settings.playlist_max_concurrent_jobs)
        )

    async def extract(self, playlist_id: str, max_tracks: int) -> PlaylistExtraction:
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
        watch = Stopwatch()
        async with self._semaphore:
            try:
                info = await asyncio.wait_for(
                    asyncio.to_thread(self._run, url, ydl_options(max_tracks)),
                    timeout=self._settings.youtube_playlist_timeout_seconds,
                )
            except TimeoutError as exc:
                raise PlaylistUpstreamUnavailable("YouTube playlist extraction timed out") from exc
        return build_extraction(playlist_id, info, max_tracks, watch.elapsed())

    def _run(self, url: str, options: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._runner(url, options)
        except PlaylistError:
            raise
        except Exception as exc:  # yt-dlp raises DownloadError/ExtractorError
            raise classify_ytdlp_error(exc) from None


def classify_ytdlp_error(exc: Exception) -> PlaylistError:
    text = str(exc).lower()
    if "does not exist" in text or "playlist is private" in text or "private playlist" in text:
        return PlaylistNotFound("The YouTube playlist does not exist or is private.")
    if "not available" in text and "playlist" in text:
        return PlaylistNotFound("The YouTube playlist is unavailable.")
    if "http error 429" in text or "sign in to confirm" in text or "not a bot" in text:
        return PlaylistUpstreamBlocked("YouTube refused the playlist request (throttled)")
    if "timed out" in text or "timeout" in text:
        return PlaylistUpstreamUnavailable("YouTube playlist extraction timed out")
    return PlaylistUpstreamUnavailable("YouTube playlist extraction failed")


def build_extraction(
    playlist_id: str, info: Any, max_tracks: int, elapsed: float = 0.0
) -> PlaylistExtraction:
    if not isinstance(info, dict) or info.get("_type") not in {"playlist", None}:
        raise PlaylistUpstreamUnavailable("yt-dlp returned an unexpected result")
    raw_entries = info.get("entries")
    if raw_entries is None:
        raise PlaylistUpstreamUnavailable("yt-dlp did not return playlist entries")
    entries = tuple(
        normalize_entry(item, index + 1)
        for index, item in enumerate(list(raw_entries)[:max_tracks])
    )
    if not entries:
        raise PlaylistEmpty()
    reported = info.get("playlist_count")
    reported_total = reported if isinstance(reported, int) and reported >= 0 else None
    truncated = reported_total is not None and reported_total > max_tracks
    complete = reported_total is not None and len(entries) == reported_total
    if truncated:
        reason = "max_tracks"
    elif complete:
        reason = None
    elif reported_total is None:
        reason = "total_unknown"
    else:
        # YouTube counts videos it hides from the listing (e.g. removed ones).
        reason = "count_mismatch"
    return PlaylistExtraction(
        source="youtube",
        playlist_id=playlist_id,
        title=_text(info.get("title")),
        description=_text(info.get("description")),
        owner=_text(info.get("channel") or info.get("uploader")),
        entries=entries,
        reported_total=reported_total,
        extraction_complete=complete and not truncated,
        extractor=EXTRACTOR_NAME,
        truncated=truncated,
        incomplete_reason=reason,
        stats=ExtractionStats(requests=0, pages=0, elapsed_seconds=elapsed),
    )


def normalize_entry(item: Any, position: int) -> PlaylistEntry:
    if not isinstance(item, dict):
        return PlaylistEntry(position, None, None, available=False, unavailable_reason="missing_item")
    video_id = item.get("id")
    if not is_valid_video_id(video_id):
        return PlaylistEntry(position, None, _text(item.get("title")), available=False, unavailable_reason="invalid_id")
    title = _text(item.get("title"))
    reason = _UNAVAILABLE_TITLES.get((title or "").casefold())
    availability = item.get("availability")
    if reason is None and isinstance(availability, str):
        reason = _UNAVAILABLE_AVAILABILITY.get(availability)
    channel = _text(item.get("channel") or item.get("uploader"))
    duration = item.get("duration")
    duration_ms = (
        round(duration * 1000)
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0
        else None
    )
    return PlaylistEntry(
        position=position,
        source_id=video_id,
        title=None if reason in {"private", "deleted"} else title,
        artists=(channel,) if channel else (),
        duration_ms=duration_ms,
        url=f"https://www.youtube.com/watch?v={video_id}",
        available=reason is None,
        unavailable_reason=reason,
    )


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
