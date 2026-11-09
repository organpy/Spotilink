"""Method B: independent Spotify web-player playlist extraction.

Mechanism (verified against the live service, see docs/v2-spotify-extraction.md):

1. Anonymous token: the `__NEXT_DATA__` session of the playlist's *own* embed
   page (`/embed/playlist/{id}`). Tokens are reused until one minute before
   their reported expiry and renewed once on HTTP 401.
2. Persisted query identifier: discovered at runtime from the web-player
   JavaScript bundle linked from open.spotify.com (Spotify serves the mobile
   web player to non-browser clients), by locating the
   `"queryPlaylist","query","<sha256>"` registration. The discovered hash is
   cached for a few hours and rediscovered once when Spotify answers HTTP 412
   ("Invalid query hash"). SPOTIFY_WEBPLAYER_QUERY_HASH pins a hash instead.
3. Query: `POST pathfinder/v2/query`, operation `queryPlaylist`, paginated with
   `offset`/`limit` (SPOTIFY_PLAYLIST_PAGE_SIZE, default 100).

Unlike Method A it hardcodes no hash or token source track, uses the v2 POST
transport and a different GraphQL operation, so a hash rotation that breaks
one implementation does not necessarily break the other. Both still depend on
api-partner.spotify.com and fail together if Spotify blocks that endpoint.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

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
    PATHFINDER_V2_URL,
    SPOTIFY_API_HOST,
    SPOTIFY_CDN_HOST,
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

EXTRACTOR_NAME = "webplayer"
OPERATION = "queryPlaylist"
_BUNDLE_RE = re.compile(
    r"https://open\.spotifycdn\.com/cdn/build/"
    r"(?:mobile-web-player/mobile-web-player|web-player/web-player)\.[0-9a-f]{6,}\.js"
)
_HASH_RE = re.compile(r'"' + OPERATION + r'","query","([0-9a-f]{64})"')
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HASH_TTL_SECONDS = 6 * 3600
_MAX_PAGES = 1_000


def find_bundle_urls(page: str) -> list[str]:
    seen: list[str] = []
    for url in _BUNDLE_RE.findall(page):
        if url not in seen:
            seen.append(url)
    return seen


def find_query_hash(bundle: str) -> str | None:
    match = _HASH_RE.search(bundle)
    return match.group(1) if match else None


class SpotifyWebPlayerExtractor:
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
        self._hash: str | None = None
        self._hash_expires_at = 0.0
        pinned = settings.spotify_webplayer_query_hash
        if pinned and not _SHA256_RE.fullmatch(pinned):
            raise ValueError("SPOTIFY_WEBPLAYER_QUERY_HASH must be a 64-char hex sha256")
        self._pinned_hash = pinned
        self._requests = 0

    async def extract(self, playlist_id: str, max_tracks: int) -> PlaylistExtraction:
        self._requests = 0
        watch = Stopwatch()
        entries: list[PlaylistEntry] = []
        header: dict[str, Any] | None = None
        page_size = self._settings.spotify_playlist_page_size
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
                limit = min(page_size, max(1, max_tracks - len(entries)))
                data = await self._query(playlist_id, offset, limit)
                union = playlist_union(data, playlist_id)
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
                "webplayer extracted %d of %d reported items for %s",
                len(entries),
                total,
                playlist_id,
            )
        return snapshot(complete, None if complete else "count_mismatch")

    async def _query(self, playlist_id: str, offset: int, limit: int) -> Any:
        rediscovered = False
        reauthenticated = False
        while True:
            token = await self._get_token(playlist_id)
            query_hash = await self._get_hash()
            self._requests += 1
            try:
                return await self._http.post_json(
                    PATHFINDER_V2_URL,
                    payload={
                        "variables": {
                            "uri": f"spotify:playlist:{playlist_id}",
                            "offset": offset,
                            "limit": limit,
                            "includeItemV3": False,
                        },
                        "operationName": OPERATION,
                        "extensions": persisted_query(OPERATION, query_hash),
                    },
                    max_bytes=self._settings.spotify_api_max_bytes,
                    timeout=self._settings.spotify_timeout_seconds,
                    allowed_hosts=frozenset({SPOTIFY_API_HOST}),
                    headers={
                        "Authorization": f"Bearer {token.value}",
                        "app-platform": "WebPlayer",
                        "Accept": "application/json",
                    },
                )
            except HttpStatusError as exc:
                if exc.status_code == 401 and not reauthenticated:
                    reauthenticated = True
                    self._token = None
                    continue
                if exc.status_code == 412 and not rediscovered and not self._pinned_hash:
                    logger.warning("Spotify rejected the cached queryPlaylist hash; rediscovering")
                    rediscovered = True
                    self._hash = None
                    continue
                raise map_http_error(exc, "webplayer query") from None
            except HttpError as exc:
                raise map_http_error(exc, "webplayer query") from None

    async def _get_token(self, playlist_id: str) -> AnonymousToken:
        now = float(self._clock())
        if self._token is not None and self._token.valid(now):
            return self._token
        self._requests += 1
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
            raise map_http_error(exc, "webplayer token") from None
        self._token = token_from_next_data(parse_next_data(page))
        return self._token

    async def _get_hash(self) -> str:
        if self._pinned_hash:
            return self._pinned_hash
        now = float(self._clock())
        if self._hash is not None and now < self._hash_expires_at:
            return self._hash
        self._requests += 1
        try:
            page = await self._http.get_text(
                f"https://{SPOTIFY_WEB_HOST}/",
                max_bytes=self._settings.spotify_max_bytes,
                timeout=self._settings.spotify_timeout_seconds,
                allowed_hosts=frozenset({SPOTIFY_WEB_HOST}),
                max_redirects=1,
                headers={"Accept": "text/html"},
            )
        except HttpError as exc:
            raise map_http_error(exc, "webplayer discovery") from None
        bundles = find_bundle_urls(page)
        if not bundles:
            raise PlaylistSchemaChanged("open.spotify.com no longer links a web-player bundle")
        for bundle_url in bundles[:2]:
            self._requests += 1
            try:
                bundle = await self._http.get_text(
                    bundle_url,
                    max_bytes=self._settings.spotify_bundle_max_bytes,
                    timeout=max(self._settings.spotify_timeout_seconds, 20.0),
                    allowed_hosts=frozenset({SPOTIFY_CDN_HOST}),
                    headers={"Accept": "*/*"},
                )
            except HttpError as exc:
                raise map_http_error(exc, "webplayer bundle") from None
            found = find_query_hash(bundle)
            if found:
                self._hash = found
                self._hash_expires_at = now + _HASH_TTL_SECONDS
                logger.info("Discovered Spotify %s persisted query hash", OPERATION)
                return found
        raise PlaylistSchemaChanged(
            f"The web-player bundle no longer registers the {OPERATION} query"
        )
