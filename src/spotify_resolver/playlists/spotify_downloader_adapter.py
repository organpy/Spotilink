"""Method A: playlist metadata extraction adapted from spotify_downloader.

Adapted from `get_playlist`, `_anon_token` and `_pathfinder` in
https://github.com/TousK74/spotify_downloader (spotify_playlist_downloader.py,
commit d670785e), Copyright (c) 2026 TousK74, MIT License:

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to permit
    persons to whom the Software is furnished to do so, subject to the
    following conditions: The above copyright notice and this permission
    notice shall be included in all copies or substantial portions of the
    Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Only the metadata path is adapted. The original's YouTube search, media
download, FFmpeg conversion and CLI are intentionally not included.

Behaviour kept from the original:
  * anonymous token from the `__NEXT_DATA__` session of a fixed track embed page,
    refreshed one minute before expiry and after HTTP 401;
  * `GET pathfinder/v1/query`, operation `fetchPlaylist`, with the original
    persisted-query hash;
  * pages of 100, advancing `offset` by the number of raw items returned and
    stopping at `content.totalCount` or an empty page.

Changes for SpotiLink: async I/O through the shared size/host-limited HTTP
client, bounded retries, Spotify track IDs and positions are kept, non-track
items are recorded instead of silently dropped, completeness is verified
against the reported total, and failures raise typed errors.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from ..config import Settings
from ..http import HttpError, HttpStatusError, LimitedHttpClient
from .models import (
    ExtractionStats,
    PlaylistEntry,
    PlaylistError,
    PlaylistExtraction,
    PlaylistSchemaChanged,
)
from .spotify_pathfinder import (
    PATHFINDER_V1_URL,
    SPOTIFY_API_HOST,
    SPOTIFY_WEB_HOST,
    AnonymousToken,
    Stopwatch,
    map_http_error,
    normalize_item,
    parse_next_data,
    persisted_query,
    playlist_header,
    playlist_union,
    token_from_next_data,
)

logger = logging.getLogger(__name__)

EXTRACTOR_NAME = "spotify_downloader"
EMBED_TOKEN_TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
FETCH_PLAYLIST_SHA256 = "a65e12194ed5fc443a1cdebed5fabe33ca5b07b987185d63c72483867ad13cb4"
PAGE_SIZE = 100
_MAX_PAGES = 1_000
_MAX_AUTH_RETRIES = 2


class SpotifyDownloaderExtractor:
    name = EXTRACTOR_NAME

    def __init__(
        self,
        http: LimitedHttpClient,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._settings = settings
        self._clock = clock
        self._token: AnonymousToken | None = None
        self._requests = 0

    async def extract(self, playlist_id: str, max_tracks: int) -> PlaylistExtraction:
        self._requests = 0
        watch = Stopwatch()
        entries: list[PlaylistEntry] = []
        header: dict | None = None
        offset = 0
        pages = 0

        def snapshot(complete: bool, reason: str | None) -> PlaylistExtraction:
            return PlaylistExtraction(
                source="spotify",
                playlist_id=playlist_id,
                title=(header or {}).get("title"),
                description=(header or {}).get("description"),
                owner=(header or {}).get("owner"),
                entries=tuple(entries),
                reported_total=(header or {}).get("total"),
                extraction_complete=complete,
                extractor=EXTRACTOR_NAME,
                truncated=reason == "max_tracks",
                incomplete_reason=None if complete else reason,
                stats=ExtractionStats(self._requests, pages, watch.elapsed()),
            )

        try:
            while pages < _MAX_PAGES:
                union = playlist_union(await self._fetch_page(playlist_id, offset), playlist_id)
                pages += 1
                if header is None:
                    header = playlist_header(union)
                items = union["content"]["items"]
                for index, item in enumerate(items):
                    if len(entries) >= max_tracks:
                        break
                    entries.append(normalize_item(item, offset + index + 1))
                offset += len(items)
                total = header["total"]
                if len(entries) >= max_tracks and total > max_tracks:
                    return snapshot(False, "max_tracks")
                if offset >= total or not items:
                    break
        except PlaylistError as exc:
            if entries:
                exc.partial = snapshot(False, exc.code)
            exc.requests = self._requests
            raise

        total = header["total"] if header else 0
        complete = len(entries) == total
        if not complete:
            logger.warning(
                "spotify_downloader extracted %d of %d reported items for %s",
                len(entries),
                total,
                playlist_id,
            )
        return snapshot(complete, None if complete else "count_mismatch")

    async def _fetch_page(self, playlist_id: str, offset: int):
        params = {
            "operationName": "fetchPlaylist",
            "variables": json.dumps(
                {
                    "uri": f"spotify:playlist:{playlist_id}",
                    "offset": offset,
                    "limit": PAGE_SIZE,
                    "enableWatchFeedEntrypoint": False,
                },
                separators=(",", ":"),
            ),
            "extensions": json.dumps(
                persisted_query("fetchPlaylist", FETCH_PLAYLIST_SHA256),
                separators=(",", ":"),
            ),
        }
        for attempt in range(_MAX_AUTH_RETRIES + 1):
            token = await self._get_token()
            self._requests += 1
            try:
                return await self._http.get_json(
                    PATHFINDER_V1_URL,
                    max_bytes=self._settings.spotify_api_max_bytes,
                    timeout=self._settings.spotify_timeout_seconds,
                    allowed_hosts=frozenset({SPOTIFY_API_HOST}),
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token.value}",
                        "app-platform": "WebPlayer",
                        "Accept": "application/json",
                    },
                )
            except HttpStatusError as exc:
                if exc.status_code == 401 and attempt < _MAX_AUTH_RETRIES:
                    self._token = None
                    continue
                raise map_http_error(exc, "spotify_downloader page") from None
            except HttpError as exc:
                raise map_http_error(exc, "spotify_downloader page") from None
        raise PlaylistSchemaChanged("Spotify kept rejecting the anonymous token")

    async def _get_token(self) -> AnonymousToken:
        now = float(self._clock())
        if self._token is not None and self._token.valid(now):
            return self._token
        self._requests += 1
        try:
            page = await self._http.get_text(
                f"https://{SPOTIFY_WEB_HOST}/embed/track/{EMBED_TOKEN_TRACK_ID}",
                max_bytes=self._settings.spotify_max_bytes,
                timeout=self._settings.spotify_timeout_seconds,
                allowed_hosts=frozenset({SPOTIFY_WEB_HOST}),
                max_redirects=1,
                headers={"Accept": "text/html"},
            )
        except HttpError as exc:
            raise map_http_error(exc, "spotify_downloader token") from None
        self._token = token_from_next_data(parse_next_data(page))
        return self._token
