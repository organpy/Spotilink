from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlsplit

from .config import Settings
from .errors import (
    InvalidSpotifyUrl,
    SpotifyMetadataError,
    SpotifyNotFound,
    SpotifyRateLimited,
    SpotifyTimeout,
    SpotifyUnavailable,
)
from .http import (
    HttpDecodeError,
    HttpNetworkError,
    HttpRedirectError,
    HttpResponseTooLarge,
    HttpStatusError,
    HttpTimeoutError,
    LimitedHttpClient,
)
from .models import SpotifyTrack

SPOTIFY_HOST = "open.spotify.com"
SPOTIFY_TRACK_PAGE = "https://open.spotify.com/track/{track_id}"
SPOTIFY_EMBED_PAGE = "https://open.spotify.com/embed/track/{track_id}"

_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_INTL_SEGMENT_RE = re.compile(r"^intl-[a-z]{2}(?:-[A-Za-z]{2})?$")
_TRACK_ID_IN_TEXT_RE = re.compile(r"track/([A-Za-z0-9]{22})(?:[/?#]|$)")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


class SpotifyMetadataIncomplete(SpotifyMetadataError):
    pass


def parse_spotify_track_url(raw: str) -> str:
    if not isinstance(raw, str):
        raise InvalidSpotifyUrl("The Spotify URL must be a string")
    value = raw.strip()
    if not value:
        raise InvalidSpotifyUrl("The Spotify URL is empty")
    if len(value) > 2048:
        raise InvalidSpotifyUrl("The Spotify URL is too long")

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise InvalidSpotifyUrl("The Spotify URL is malformed") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidSpotifyUrl("Only HTTP(S) Spotify track URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidSpotifyUrl(
            "Spotify URLs with embedded credentials are not supported"
        )
    if port is not None and port not in {80, 443}:
        raise InvalidSpotifyUrl("Spotify URLs with custom ports are not supported")
    if (parsed.hostname or "").lower().rstrip(".") != SPOTIFY_HOST:
        raise InvalidSpotifyUrl("Only open.spotify.com track URLs are supported")

    path = unquote(parsed.path)
    segments = [segment for segment in path.split("/") if segment]
    if segments and _INTL_SEGMENT_RE.match(segments[0]):
        segments = segments[1:]
    if len(segments) != 2 or segments[0] != "track":
        raise InvalidSpotifyUrl(
            "The URL is not a Spotify track URL (albums, playlists, artists, and other types are not supported)"
        )
    track_id = segments[1]
    if not _TRACK_ID_RE.fullmatch(track_id):
        raise InvalidSpotifyUrl("The Spotify track ID must be 22 base62 characters")
    return track_id


@dataclass(frozen=True, slots=True)
class _Fields:
    title: str | None = None
    artists: tuple[str, ...] = ()
    album: str | None = None
    duration_ms: int | None = None
    duration_precision: str | None = None


class _ScriptTooLarge(Exception):
    pass


class _PageParser(HTMLParser):
    def __init__(self, max_script_chars: int = 2_097_152) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.jsonld: list[str] = []
        self.scripts: dict[str, str] = {}
        self._max_script_chars = max_script_chars
        self._script_kind: str | None = None
        self._script_buffer: list[str] = []
        self._script_size = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            content = values.get("content")
            if key and content is not None:
                self.meta.setdefault(key.lower(), content.strip())
            return
        if tag != "script":
            return

        script_type = values.get("type", "").lower().split(";", 1)[0].strip()
        script_id = values.get("id", "")
        if script_type == "application/ld+json":
            kind = "jsonld"
        elif script_id == "initialState":
            kind = "initialState"
        elif script_id == "__NEXT_DATA__":
            kind = "__NEXT_DATA__"
        else:
            kind = None
        if kind is None:
            return
        self._script_kind = kind
        self._script_buffer = []
        self._script_size = 0

    def handle_data(self, data: str) -> None:
        if self._script_kind is None:
            return
        self._script_size += len(data)
        if self._script_size > self._max_script_chars:
            raise _ScriptTooLarge("Embedded metadata script exceeded the size limit")
        self._script_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "script" or self._script_kind is None:
            return
        content = "".join(self._script_buffer)
        if self._script_kind == "jsonld":
            self.jsonld.append(content)
        else:
            self.scripts[self._script_kind] = content
        self._script_kind = None
        self._script_buffer = []
        self._script_size = 0


def parse_spotify_page(html: str, track_id: str) -> SpotifyTrack:
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
    except _ScriptTooLarge as exc:
        raise SpotifyMetadataError(
            "Spotify embedded metadata exceeded the size limit"
        ) from exc
    except Exception as exc:
        raise SpotifyMetadataError("Spotify page HTML could not be parsed") from exc

    state = _fields_from_initial_state(parser.scripts.get("initialState"), track_id)
    next_data = _fields_from_next_data(parser.scripts.get("__NEXT_DATA__"), track_id)
    jsonld = _fields_from_jsonld(parser.jsonld, track_id)
    meta = _fields_from_meta(parser.meta)

    title = _first_text(jsonld.title, state.title, next_data.title, meta.title)
    artists = _first_artists(
        state.artists, next_data.artists, jsonld.artists, meta.artists
    )
    album = _first_text(state.album, next_data.album, jsonld.album, meta.album)

    duration_ms: int | None = None
    duration_precision: str | None = None
    for fields in (state, next_data, jsonld):
        if fields.duration_ms is None or fields.duration_ms <= 0:
            continue
        if duration_ms is None or fields.duration_precision == "ms":
            duration_ms = fields.duration_ms
            duration_precision = fields.duration_precision
        if duration_precision == "ms":
            break
    if duration_ms is None and meta.duration_ms is not None:
        duration_ms = meta.duration_ms
        duration_precision = meta.duration_precision

    if not title or not artists:
        raise SpotifyMetadataIncomplete(
            "Spotify did not expose both a track title and artist metadata"
        )

    if duration_ms is not None and duration_ms <= 0:
        duration_ms = None
        duration_precision = None

    return SpotifyTrack(
        spotify_id=track_id,
        title=title,
        artists=artists,
        album=album,
        duration_ms=duration_ms,
        duration_precision=duration_precision,
    )


def _fields_from_meta(meta: dict[str, str]) -> _Fields:
    title = _first_text(meta.get("og:title"), meta.get("twitter:title"))
    artist = meta.get("music:musician_description")
    album: str | None = None
    description = meta.get("og:description")
    description_artist: str | None = None
    if description:
        parts = [part.strip() for part in description.split("·")]
        if len(parts) >= 3 and parts[2].casefold() in {"song", "track"}:
            description_artist = _clean_text(parts[0])
            album = _clean_text(parts[1]) if len(parts) > 1 else None

    duration_ms: int | None = None
    precision: str | None = None
    raw_duration = meta.get("music:duration")
    if raw_duration:
        try:
            seconds = float(raw_duration)
            if math.isfinite(seconds) and seconds > 0:
                duration_ms = round(seconds * 1000)
                precision = _seconds_precision(raw_duration)
        except ValueError:
            pass

    artists = _dedupe_artists([artist] if artist else [])
    if not artists and description_artist:
        artists = (description_artist,)
    return _Fields(
        title=title,
        artists=artists,
        album=album,
        duration_ms=duration_ms,
        duration_precision=precision,
    )


def _fields_from_jsonld(raw_scripts: Iterable[str], track_id: str) -> _Fields:
    best: _Fields | None = None
    for raw in raw_scripts:
        try:
            document = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            continue
        for obj in _walk_dicts(document):
            types = obj.get("@type")
            if isinstance(types, str):
                types = [types]
            if not isinstance(types, list) or "MusicRecording" not in types:
                continue
            object_id = obj.get("@id") or obj.get("url")
            if isinstance(object_id, str):
                match = _TRACK_ID_IN_TEXT_RE.search(object_id)
                if match and match.group(1) != track_id:
                    continue
            title = _clean_text(obj.get("name"))
            artists = _artists_from_json(obj.get("byArtist"))
            album = _album_from_json(obj.get("inAlbum"))
            raw_duration = obj.get("duration")
            duration_ms = _parse_iso8601_duration(raw_duration)
            fields = _Fields(
                title=title,
                artists=artists,
                album=album,
                duration_ms=duration_ms,
                duration_precision=(
                    _iso_duration_precision(raw_duration)
                    if duration_ms is not None
                    else None
                ),
            )
            if fields.title:
                if best is None:
                    best = fields
                elif not best.artists and fields.artists:
                    best = _Fields(
                        title=best.title or fields.title,
                        artists=fields.artists,
                        album=best.album or fields.album,
                        duration_ms=best.duration_ms or fields.duration_ms,
                        duration_precision=best.duration_precision
                        or fields.duration_precision,
                    )
                if best.artists and best.duration_ms is not None:
                    return best
    return best or _Fields()


def _fields_from_initial_state(raw: str | None, track_id: str) -> _Fields:
    if not raw:
        return _Fields()
    document = _decode_embedded_json(raw)
    if not isinstance(document, dict):
        return _Fields()
    entities = document.get("entities")
    if isinstance(entities, dict):
        items = entities.get("items")
        if isinstance(items, dict):
            candidate = items.get(f"spotify:track:{track_id}")
            if (
                isinstance(candidate, dict)
                and _entity_matches(candidate, track_id)
                and _entity_has_metadata(candidate)
            ):
                return _fields_from_entity(candidate)
    entity = _find_entity(document, track_id)
    return _fields_from_entity(entity) if entity is not None else _Fields()


def _fields_from_next_data(raw: str | None, track_id: str) -> _Fields:
    if not raw:
        return _Fields()
    document = _decode_embedded_json(raw)
    if not isinstance(document, dict):
        return _Fields()
    try:
        entity = document["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError):
        entity = None
    if (
        isinstance(entity, dict)
        and _entity_matches(entity, track_id)
        and _entity_has_metadata(entity)
    ):
        return _fields_from_entity(entity)
    entity = _find_entity(document, track_id)
    return _fields_from_entity(entity) if entity is not None else _Fields()


def _artist_names(value: Any) -> list[str]:
    if isinstance(value, dict):
        items = value.get("items")
        raw_items: Any = items if isinstance(items, list) else [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []

    names: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            names.append(item)
            continue
        if not isinstance(item, dict):
            continue
        profile = item.get("profile")
        profile_name = profile.get("name") if isinstance(profile, dict) else None
        item_name = item.get("name")
        if isinstance(profile_name, str) and profile_name.strip():
            names.append(profile_name)
        elif isinstance(item_name, str) and item_name.strip():
            names.append(item_name)
    return names


def _fields_from_entity(entity: dict[str, Any]) -> _Fields:
    title = _clean_text(entity.get("name") or entity.get("title"))
    artists: list[str] = []
    for key in ("artists", "firstArtist", "otherArtists"):
        artists.extend(_artist_names(entity.get(key)))

    duration = entity.get("duration")
    duration_ms: int | None = None
    if isinstance(duration, dict):
        raw_ms = duration.get("totalMilliseconds")
        if _finite_number(raw_ms):
            duration_ms = int(raw_ms)
    elif _finite_number(duration):
        duration_ms = int(duration)
    elif _finite_number(entity.get("duration_ms")):
        duration_ms = int(entity["duration_ms"])

    album: str | None = None
    for key in ("albumOfTrack", "album", "inAlbum"):
        value = entity.get(key)
        if isinstance(value, dict) and value.get("name"):
            album = _clean_text(value["name"])
            break

    return _Fields(
        title=title,
        artists=_dedupe_artists(artists),
        album=album,
        duration_ms=duration_ms,
        duration_precision="ms" if duration_ms is not None else None,
    )


def _artists_from_json(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return _dedupe_artists([value])
    if isinstance(value, dict):
        if value.get("name"):
            return _dedupe_artists([str(value["name"])])
        return ()
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        return _dedupe_artists(names)
    return ()


def _album_from_json(value: Any) -> str | None:
    if isinstance(value, dict):
        return _clean_text(value.get("name"))
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        for item in value:
            album = _album_from_json(item)
            if album:
                return album
    return None


def _decode_embedded_json(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return None
    if value.startswith(("{", "[")):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return None
    for decoder in (
        base64.b64decode,
        base64.urlsafe_b64decode,
    ):
        try:
            if decoder is base64.b64decode:
                decoded = decoder(value.encode("ascii"), validate=False)
            else:
                decoded = decoder(value.encode("ascii"))
            text = decoded.decode("utf-8")
            return json.loads(text)
        except (
            binascii.Error,
            UnicodeDecodeError,
            UnicodeEncodeError,
            ValueError,
            RecursionError,
        ):
            continue
    return None


def _find_entity(root: Any, track_id: str) -> dict[str, Any] | None:
    stack: list[tuple[Any, int]] = [(root, 0)]
    visited = 0
    while stack:
        node, depth = stack.pop()
        if depth > 30 or visited > 200_000:
            return None
        visited += 1
        if isinstance(node, dict):
            if _entity_matches(node, track_id) and _entity_has_metadata(node):
                return node
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
    return None


def _entity_matches(entity: dict[str, Any], track_id: str) -> bool:
    if entity.get("id") == track_id:
        return True
    uri = entity.get("uri")
    return bool(isinstance(uri, str) and uri.endswith(f":track:{track_id}"))


def _entity_has_metadata(entity: dict[str, Any]) -> bool:
    return bool(
        entity.get("name")
        or entity.get("title")
        or entity.get("artists")
        or entity.get("firstArtist")
        or entity.get("otherArtists")
        or entity.get("duration")
        or entity.get("duration_ms")
    )


def _walk_dicts(root: Any) -> Iterable[dict[str, Any]]:
    stack: list[tuple[Any, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > 15:
            continue
        if isinstance(node, dict):
            yield node
            for value in node.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _seconds_precision(value: str) -> str:
    text = value.strip()
    if "." not in text:
        return "s"
    fraction = text.split(".", 1)[1]
    return "ms" if any(char not in "0" for char in fraction) else "s"


def _iso_duration_precision(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _ISO_DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    seconds = match.group("seconds")
    if seconds and "." in seconds:
        fraction = seconds.split(".", 1)[1]
        return "ms" if any(char != "0" for char in fraction) else "s"
    return "s"


def _parse_iso8601_duration(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = _ISO_DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    try:
        total = days * 86_400 + hours * 3_600 + minutes * 60 + seconds
    except OverflowError:
        return None
    if not math.isfinite(total) or total <= 0:
        return None
    return round(total * 1000)


def _dedupe_artists(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    artists: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        artists.append(cleaned)
    return tuple(artists)


def _first_text(*values: str | None) -> str | None:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return None


def _first_artists(*values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        if value:
            return value
    return ()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class SpotifyClient:
    def __init__(self, http: LimitedHttpClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings
        self._headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def get_track(self, track_id: str) -> SpotifyTrack:
        page_url = SPOTIFY_TRACK_PAGE.format(track_id=track_id)
        main_error: SpotifyMetadataError | None = None
        track: SpotifyTrack | None = None

        try:
            html = await self._fetch_html(page_url)
            track = parse_spotify_page(html, track_id)
        except SpotifyMetadataError as exc:
            main_error = exc

        if track is None:
            try:
                embed_html = await self._fetch_html(
                    SPOTIFY_EMBED_PAGE.format(track_id=track_id),
                    max_redirects=1,
                )
                track = parse_spotify_page(embed_html, track_id)
            except (SpotifyMetadataError, SpotifyNotFound) as exc:
                raise SpotifyMetadataError(
                    "Spotify did not expose usable track metadata"
                ) from (main_error or exc)
        elif track.duration_ms is None:
            try:
                embed_html = await self._fetch_html(
                    SPOTIFY_EMBED_PAGE.format(track_id=track_id),
                    max_redirects=1,
                )
                embedded = parse_spotify_page(embed_html, track_id)
                track = SpotifyTrack(
                    spotify_id=track.spotify_id,
                    title=track.title,
                    artists=track.artists,
                    album=track.album or embedded.album,
                    duration_ms=embedded.duration_ms,
                    duration_precision=embedded.duration_precision,
                )
            except (
                SpotifyMetadataError,
                SpotifyNotFound,
                SpotifyRateLimited,
                SpotifyTimeout,
                SpotifyUnavailable,
            ):
                pass

        return track

    async def _fetch_html(self, url: str, max_redirects: int | None = None) -> str:
        try:
            return await self._http.get_text(
                url,
                max_bytes=self._settings.spotify_max_bytes,
                timeout=self._settings.spotify_timeout_seconds,
                allowed_hosts=frozenset({SPOTIFY_HOST}),
                max_redirects=(
                    self._settings.spotify_max_redirects
                    if max_redirects is None
                    else max_redirects
                ),
                headers=self._headers,
            )
        except HttpStatusError as exc:
            if exc.status_code == 404:
                raise SpotifyNotFound() from exc
            if exc.status_code == 429:
                raise SpotifyRateLimited(exc.retry_after) from exc
            if exc.status_code >= 500:
                raise SpotifyUnavailable() from exc
            raise SpotifyMetadataError(
                "Spotify rejected the public track page request"
            ) from exc
        except HttpTimeoutError as exc:
            raise SpotifyTimeout() from exc
        except HttpNetworkError as exc:
            raise SpotifyUnavailable() from exc
        except HttpResponseTooLarge as exc:
            raise SpotifyMetadataError(
                "Spotify track page exceeded the size limit"
            ) from exc
        except HttpRedirectError as exc:
            raise SpotifyMetadataError(
                "Spotify track page redirected outside its allowed host"
            ) from exc
        except HttpDecodeError as exc:
            raise SpotifyMetadataError(
                "Spotify track page could not be decoded"
            ) from exc
