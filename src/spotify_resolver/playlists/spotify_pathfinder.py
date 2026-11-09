"""Shared helpers for Spotify's anonymous embed session and pathfinder responses.

Both playlist extractors (the spotify_downloader adaptation and the
independent web-player implementation) talk to the same upstream GraphQL
gateway and receive the same `playlistV2` union, so token parsing, response
validation and item normalization live here once. Transport, token source,
query identifiers and pagination strategy remain per implementation.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..http import (
    HttpDecodeError,
    HttpError,
    HttpNetworkError,
    HttpRedirectError,
    HttpResponseTooLarge,
    HttpStatusError,
    HttpTimeoutError,
)
from .models import (
    PlaylistEntry,
    PlaylistError,
    PlaylistNotFound,
    PlaylistSchemaChanged,
    PlaylistUpstreamBlocked,
    PlaylistUpstreamUnavailable,
)

SPOTIFY_WEB_HOST = "open.spotify.com"
SPOTIFY_API_HOST = "api-partner.spotify.com"
SPOTIFY_CDN_HOST = "open.spotifycdn.com"
PATHFINDER_V1_URL = f"https://{SPOTIFY_API_HOST}/pathfinder/v1/query"
PATHFINDER_V2_URL = f"https://{SPOTIFY_API_HOST}/pathfinder/v2/query"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_TRACK_URI_RE = re.compile(r"^spotify:track:([A-Za-z0-9]{22})$")
_TOKEN_SAFETY_MARGIN_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class AnonymousToken:
    value: str
    expires_at: float  # epoch seconds

    def valid(self, now: float) -> bool:
        return now < self.expires_at - _TOKEN_SAFETY_MARGIN_SECONDS

    def __repr__(self) -> str:  # never leak the bearer token into logs
        return f"AnonymousToken(expires_at={self.expires_at:.0f})"


def parse_next_data(page: str) -> dict[str, Any]:
    match = _NEXT_DATA_RE.search(page)
    if not match:
        raise PlaylistSchemaChanged("Spotify embed page no longer exposes __NEXT_DATA__")
    try:
        document = json.loads(match.group(1))
    except (ValueError, RecursionError) as exc:
        raise PlaylistSchemaChanged("Spotify embed __NEXT_DATA__ is not valid JSON") from exc
    if not isinstance(document, dict):
        raise PlaylistSchemaChanged("Spotify embed __NEXT_DATA__ has an unexpected shape")
    return document


def token_from_next_data(document: dict[str, Any]) -> AnonymousToken:
    try:
        session = document["props"]["pageProps"]["state"]["settings"]["session"]
        token = session["accessToken"]
        expires_ms = session["accessTokenExpirationTimestampMs"]
    except (KeyError, TypeError) as exc:
        raise PlaylistSchemaChanged(
            "Spotify embed page no longer exposes an anonymous session token"
        ) from exc
    if not isinstance(token, str) or not token or not isinstance(expires_ms, int):
        raise PlaylistSchemaChanged("Spotify anonymous session token has an unexpected shape")
    return AnonymousToken(token, expires_ms / 1000.0)


def persisted_query(operation: str, sha256: str) -> dict[str, Any]:
    return {"persistedQuery": {"version": 1, "sha256Hash": sha256}}


def playlist_union(data: Any, playlist_id: str) -> dict[str, Any]:
    """Validate a pathfinder response and return its `playlistV2` object."""
    if not isinstance(data, dict):
        raise PlaylistSchemaChanged("Spotify returned a non-object GraphQL response")
    errors = data.get("errors")
    payload = data.get("data")
    union = payload.get("playlistV2") if isinstance(payload, dict) else None
    if not isinstance(union, dict):
        if errors:
            raise PlaylistSchemaChanged(
                "Spotify rejected the playlist query: " + _first_error_message(errors)
            )
        raise PlaylistSchemaChanged("Spotify response did not contain playlistV2")
    typename = union.get("__typename")
    if typename == "NotFound":
        raise PlaylistNotFound()
    if typename == "GenericError":
        message = str(union.get("message") or "")
        if "400 BAD_REQUEST" in message or "403" in message or "404" in message:
            raise PlaylistNotFound(
                "The playlist does not exist, is private or cannot be accessed."
            )
        raise PlaylistUpstreamUnavailable(
            "Spotify reported a playlist error: " + message[:200]
        )
    if typename != "Playlist":
        raise PlaylistSchemaChanged(
            f"Spotify returned an unexpected playlistV2 type: {typename!r}"
        )
    content = union.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("items"), list):
        raise PlaylistSchemaChanged("Spotify playlist content has an unexpected shape")
    total = content.get("totalCount")
    if not isinstance(total, int) or total < 0:
        raise PlaylistSchemaChanged("Spotify playlist totalCount is missing")
    return union


def playlist_header(union: dict[str, Any]) -> dict[str, Any]:
    owner = None
    owner_v2 = union.get("ownerV2")
    if isinstance(owner_v2, dict) and isinstance(owner_v2.get("data"), dict):
        owner = _clean(owner_v2["data"].get("name"))
    description = _clean(union.get("description"))
    if description:
        description = html.unescape(re.sub(r"<[^>]+>", "", description))
    return {
        "title": _clean(union.get("name")),
        "description": description,
        "owner": owner,
        "total": union["content"]["totalCount"],
    }


def normalize_item(item: Any, position: int) -> PlaylistEntry:
    """Convert one pathfinder playlist item into a PlaylistEntry.

    Non-track and metadata-less items are kept as unavailable entries so
    positions and counts remain faithful to the source playlist.
    """
    wrapper = item.get("itemV2") if isinstance(item, dict) else None
    data = wrapper.get("data") if isinstance(wrapper, dict) else None
    if not isinstance(data, dict):
        return PlaylistEntry(
            position, None, None, available=False, unavailable_reason="missing_item"
        )
    typename = data.get("__typename")
    uri = data.get("uri") if isinstance(data.get("uri"), str) else None
    if typename != "Track":
        reason = {
            "Episode": "podcast_episode",
            "LocalTrack": "local_file",
            "NotFound": "removed",
            "RestrictedContent": "restricted",
        }.get(str(typename), "not_a_track")
        return PlaylistEntry(
            position,
            None,
            _clean(data.get("name")),
            available=False,
            unavailable_reason=reason,
        )
    match = _TRACK_URI_RE.match(uri or "")
    track_id = match.group(1) if match else None
    title = _clean(data.get("name"))
    artists = _artist_names(data.get("artists"))
    duration = data.get("trackDuration") or data.get("duration")
    duration_ms: int | None = None
    if isinstance(duration, dict) and isinstance(duration.get("totalMilliseconds"), int):
        duration_ms = duration["totalMilliseconds"] or None
    album_data = data.get("albumOfTrack")
    album = _clean(album_data.get("name")) if isinstance(album_data, dict) else None
    isrc = _isrc(data)
    if not track_id or not title or not artists:
        return PlaylistEntry(
            position,
            track_id,
            title,
            artists,
            duration_ms,
            album,
            isrc,
            available=False,
            # Spotify keeps the URI of tracks removed from its catalogue but
            # returns no name or artists for them.
            unavailable_reason=(
                "removed" if track_id and not title and not artists else "incomplete_metadata"
            ),
        )
    return PlaylistEntry(
        position=position,
        source_id=track_id,
        title=title,
        artists=artists,
        duration_ms=duration_ms,
        album=album,
        isrc=isrc,
        url=f"https://open.spotify.com/track/{track_id}",
    )


def map_http_error(exc: HttpError, context: str) -> PlaylistError:
    if isinstance(exc, HttpStatusError):
        if exc.status_code == 412:
            return PlaylistSchemaChanged(
                f"{context}: Spotify rejected the persisted query hash (HTTP 412)"
            )
        if exc.status_code == 429:
            return PlaylistUpstreamBlocked(
                f"{context}: Spotify rate-limited the request",
                retry_after_seconds=exc.retry_after,
            )
        if exc.status_code == 403:
            return PlaylistUpstreamBlocked(f"{context}: Spotify denied access (HTTP 403)")
        if exc.status_code == 404:
            return PlaylistNotFound()
        if exc.status_code >= 500:
            return PlaylistUpstreamUnavailable(
                f"{context}: Spotify returned HTTP {exc.status_code}"
            )
        return PlaylistSchemaChanged(f"{context}: Spotify returned HTTP {exc.status_code}")
    if isinstance(exc, HttpTimeoutError):
        return PlaylistUpstreamUnavailable(f"{context}: request timed out")
    if isinstance(exc, HttpNetworkError):
        return PlaylistUpstreamUnavailable(f"{context}: network error")
    if isinstance(exc, HttpResponseTooLarge):
        return PlaylistSchemaChanged(f"{context}: response exceeded the size limit")
    if isinstance(exc, HttpRedirectError):
        return PlaylistSchemaChanged(f"{context}: unexpected redirect")
    if isinstance(exc, HttpDecodeError):
        return PlaylistSchemaChanged(f"{context}: malformed JSON")
    return PlaylistUpstreamUnavailable(f"{context}: request failed")


class Stopwatch:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._start = clock()

    def elapsed(self) -> float:
        return round(self._clock() - self._start, 3)


def _artist_names(value: Any) -> tuple[str, ...]:
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list):
        return ()
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        profile = item.get("profile")
        name = profile.get("name") if isinstance(profile, dict) else item.get("name")
        cleaned = _clean(name)
        if cleaned and cleaned not in names:
            names.append(cleaned)
    return tuple(names)


def _isrc(data: dict[str, Any]) -> str | None:
    external = data.get("externalIds") or data.get("external_ids")
    if isinstance(external, dict):
        value = external.get("isrc")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _first_error_message(errors: Any) -> str:
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "unknown error")[:200]
    return "unknown error"


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
