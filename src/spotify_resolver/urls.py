"""Strict classification of v2 input URLs.

Only an explicit allowlist of hosts and path shapes is accepted. The input URL
itself is never fetched: callers receive a validated identifier and build a
canonical upstream URL from it, which rules out SSRF through user input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, unquote, urlsplit

SPOTIFY_HOSTS = frozenset({"open.spotify.com"})
YOUTUBE_HOSTS = frozenset(
    {"www.youtube.com", "youtube.com", "m.youtube.com", "music.youtube.com"}
)
YOUTU_BE_HOSTS = frozenset({"youtu.be", "www.youtu.be"})

_SPOTIFY_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_INTL_SEGMENT_RE = re.compile(r"^intl-[a-z]{2}(?:-[A-Za-z]{2})?$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Public, stable playlists only: user playlists (PL), album playlists (OLAK5uy_),
# channel uploads (UU) and legacy favourites (FL). Auto-generated mixes (RD...),
# Watch Later (WL) and Liked (LL) are private or unbounded and are rejected.
_PLAYLIST_ID_RE = re.compile(r"^(?:PL|OLAK5uy_|UU|FL)[A-Za-z0-9_-]{10,80}$")


class InputKind(StrEnum):
    SPOTIFY_TRACK = "spotify_track"
    SPOTIFY_PLAYLIST = "spotify_playlist"
    YOUTUBE_PLAYLIST = "youtube_playlist"


class UnsupportedInput(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ClassifiedInput:
    kind: InputKind
    identifier: str

    @property
    def source(self) -> str:
        return "youtube" if self.kind is InputKind.YOUTUBE_PLAYLIST else "spotify"

    @property
    def canonical_url(self) -> str:
        if self.kind is InputKind.SPOTIFY_TRACK:
            return f"https://open.spotify.com/track/{self.identifier}"
        if self.kind is InputKind.SPOTIFY_PLAYLIST:
            return f"https://open.spotify.com/playlist/{self.identifier}"
        return f"https://www.youtube.com/playlist?list={self.identifier}"


def classify_url(raw: object) -> ClassifiedInput:
    if not isinstance(raw, str):
        raise UnsupportedInput("invalid_url", "The URL must be a string.")
    value = raw.strip()
    if not value:
        raise UnsupportedInput("invalid_url", "The URL is empty.")
    if len(value) > 2048:
        raise UnsupportedInput("invalid_url", "The URL is too long.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UnsupportedInput("invalid_url", "The URL is malformed.") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsupportedInput("invalid_url", "Only http(s) URLs are supported.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsupportedInput(
            "invalid_url", "URLs with embedded credentials are not supported."
        )
    if port is not None and port not in {80, 443}:
        raise UnsupportedInput("invalid_url", "URLs with custom ports are not supported.")

    host = (parsed.hostname or "").lower().rstrip(".")
    if host in SPOTIFY_HOSTS:
        return _classify_spotify(parsed.path)
    if host in YOUTUBE_HOSTS:
        return _classify_youtube(parsed.path, parsed.query)
    if host in YOUTU_BE_HOSTS:
        return _classify_youtu_be(parsed.query)
    raise UnsupportedInput(
        "unsupported_input",
        "Only open.spotify.com, youtube.com, music.youtube.com and youtu.be URLs are supported.",
    )


def _classify_spotify(path: str) -> ClassifiedInput:
    segments = [segment for segment in unquote(path).split("/") if segment]
    if segments and _INTL_SEGMENT_RE.match(segments[0]):
        segments = segments[1:]
    if len(segments) != 2 or segments[0] not in {"track", "playlist"}:
        raise UnsupportedInput(
            "unsupported_input",
            "Only Spotify track and playlist URLs are supported.",
        )
    identifier = segments[1]
    if not _SPOTIFY_ID_RE.fullmatch(identifier):
        raise UnsupportedInput(
            "invalid_url", "The Spotify ID must be 22 base62 characters."
        )
    kind = (
        InputKind.SPOTIFY_TRACK if segments[0] == "track" else InputKind.SPOTIFY_PLAYLIST
    )
    return ClassifiedInput(kind, identifier)


def _playlist_from_query(query: str) -> str | None:
    values = parse_qs(query, keep_blank_values=False).get("list") or []
    if not values:
        return None
    list_id = values[0].strip()
    if not _PLAYLIST_ID_RE.fullmatch(list_id):
        raise UnsupportedInput(
            "unsupported_input",
            "The YouTube list identifier is not a supported public playlist "
            "(mixes, Watch Later and Liked videos are not supported).",
        )
    return list_id


def _classify_youtube(path: str, query: str) -> ClassifiedInput:
    segments = [segment for segment in path.split("/") if segment]
    list_id = _playlist_from_query(query)
    if list_id is not None and segments in (["playlist"], ["watch"], ["browse"]):
        return ClassifiedInput(InputKind.YOUTUBE_PLAYLIST, list_id)
    if list_id is not None and not segments:
        return ClassifiedInput(InputKind.YOUTUBE_PLAYLIST, list_id)
    if segments == ["watch"] or (segments and segments[0] in {"shorts", "live", "embed"}):
        raise UnsupportedInput(
            "youtube_video_not_supported",
            "Individual YouTube videos are not supported; submit a playlist URL.",
        )
    raise UnsupportedInput(
        "unsupported_input", "The YouTube URL does not contain a playlist identifier."
    )


def _classify_youtu_be(query: str) -> ClassifiedInput:
    list_id = _playlist_from_query(query)
    if list_id is not None:
        return ClassifiedInput(InputKind.YOUTUBE_PLAYLIST, list_id)
    raise UnsupportedInput(
        "youtube_video_not_supported",
        "Individual YouTube videos are not supported; submit a playlist URL.",
    )


def is_valid_video_id(value: object) -> bool:
    return isinstance(value, str) and bool(_VIDEO_ID_RE.fullmatch(value))
