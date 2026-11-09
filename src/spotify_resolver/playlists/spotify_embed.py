"""Last-resort partial extractor: the public playlist embed page.

The embed page server-renders only the first 100 tracks, joins artist names
into one display string and does not report the playlist size. Results are
therefore always reported as incomplete (`extraction_complete=False`) and are
used only when both GraphQL implementations fail.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import Settings
from ..http import HttpError, LimitedHttpClient
from .models import (
    ExtractionStats,
    PlaylistEmpty,
    PlaylistEntry,
    PlaylistExtraction,
    PlaylistNotFound,
    PlaylistSchemaChanged,
)
from .spotify_pathfinder import SPOTIFY_WEB_HOST, Stopwatch, map_http_error, parse_next_data

EXTRACTOR_NAME = "spotify_embed"
_TRACK_URI_RE = re.compile(r"^spotify:track:([A-Za-z0-9]{22})$")


class SpotifyEmbedExtractor:
    name = EXTRACTOR_NAME

    def __init__(self, http: LimitedHttpClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings

    async def extract(
        self,
        playlist_id: str,
        max_tracks: int,
        *,
        reported_total: int | None = None,
    ) -> PlaylistExtraction:
        watch = Stopwatch()
        try:
            page = await self._http.get_text(
                f"https://{SPOTIFY_WEB_HOST}/embed/playlist/{playlist_id}",
                max_bytes=self._settings.spotify_max_bytes,
                timeout=self._settings.spotify_timeout_seconds,
                allowed_hosts=frozenset({SPOTIFY_WEB_HOST}),
                max_redirects=1,
                headers={"Accept": "text/html"},
            )
        except HttpError as exc:
            raise map_http_error(exc, "embed page") from None
        document = parse_next_data(page)
        page_props = (document.get("props") or {}).get("pageProps")
        if isinstance(page_props, dict) and page_props.get("status") == 404:
            raise PlaylistNotFound()
        try:
            entity = document["props"]["pageProps"]["state"]["data"]["entity"]
        except (KeyError, TypeError) as exc:
            raise PlaylistSchemaChanged("Playlist embed page has no entity") from exc
        if not isinstance(entity, dict):
            raise PlaylistSchemaChanged("Playlist embed entity has an unexpected shape")
        raw_tracks = entity.get("trackList")
        if not isinstance(raw_tracks, list):
            raise PlaylistSchemaChanged("Playlist embed page has no trackList")
        if not raw_tracks:
            raise PlaylistEmpty()
        entries = tuple(
            _entry(item, index + 1) for index, item in enumerate(raw_tracks[:max_tracks])
        )
        complete = reported_total is not None and len(entries) == reported_total
        return PlaylistExtraction(
            source="spotify",
            playlist_id=playlist_id,
            title=_text(entity.get("name") or entity.get("title")),
            owner=_text(entity.get("subtitle")),
            entries=entries,
            reported_total=reported_total,
            extraction_complete=complete,
            extractor=EXTRACTOR_NAME,
            incomplete_reason=None if complete else "embed_first_page_only",
            stats=ExtractionStats(1, 1, watch.elapsed()),
        )


def _entry(item: Any, position: int) -> PlaylistEntry:
    if not isinstance(item, dict):
        return PlaylistEntry(position, None, None, available=False, unavailable_reason="missing_item")
    match = _TRACK_URI_RE.match(str(item.get("uri") or ""))
    title = _text(item.get("title"))
    subtitle = _text(item.get("subtitle")) or ""
    # The embed joins artists with ", "; names containing commas cannot be
    # recovered exactly from this source.
    artists = tuple(part.strip() for part in subtitle.split(",") if part.strip())
    duration = item.get("duration")
    duration_ms = duration if isinstance(duration, int) and duration > 0 else None
    if not match or not title or not artists:
        return PlaylistEntry(
            position,
            match.group(1) if match else None,
            title,
            artists,
            duration_ms,
            available=False,
            unavailable_reason="incomplete_metadata",
        )
    track_id = match.group(1)
    return PlaylistEntry(
        position=position,
        source_id=track_id,
        title=title,
        artists=artists,
        duration_ms=duration_ms,
        url=f"https://open.spotify.com/track/{track_id}",
    )


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None
