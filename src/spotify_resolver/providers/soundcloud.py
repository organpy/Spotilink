from __future__ import annotations

import asyncio
import html
import math
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..concurrency import AsyncRateLimiter
from ..config import Settings
from ..errors import (
    ProviderAuthError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUpstreamError,
)
from ..http import (
    HttpDecodeError,
    HttpNetworkError,
    HttpRedirectError,
    HttpResponseTooLarge,
    HttpStatusError,
    HttpTimeoutError,
    LimitedHttpClient,
)
from ..models import Candidate
from .base import Provider

SOUNDCLOUD_TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
SOUNDCLOUD_API_BASE = "https://api.soundcloud.com"
SOUNDCLOUD_TOKEN_HOST = "secure.soundcloud.com"
SOUNDCLOUD_API_HOST = "api.soundcloud.com"

_RESERVED_FIRST_SEGMENTS = {
    "albums",
    "charts",
    "discover",
    "followers",
    "following",
    "im",
    "jobs",
    "likes",
    "messages",
    "pages",
    "playlists",
    "privacy",
    "search",
    "settings",
    "stream",
    "tags",
    "terms",
    "upload",
    "you",
}
_RESERVED_SECOND_SEGMENTS = {
    "albums",
    "likes",
    "playlists",
    "reposts",
    "sets",
    "tracks",
}
_TRACK_PATH_RE = re.compile(r"^/[^/]+/[^/]+(?:/[^/]+)*$")


class SoundCloudProvider(Provider):
    name = "soundcloud"

    def __init__(
        self,
        http: LimitedHttpClient,
        settings: Settings,
        *,
        limiter: AsyncRateLimiter | None = None,
        semaphore: asyncio.Semaphore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._settings = settings
        self._client_id = settings.soundcloud_client_id
        self._client_secret = settings.soundcloud_client_secret
        self._timeout = settings.provider_timeout_seconds
        self._max_bytes = settings.provider_max_bytes
        self._max_results = min(200, settings.soundcloud_max_results)
        self._limiter = limiter or AsyncRateLimiter(
            settings.soundcloud_rate_limit_per_minute,
            burst=settings.soundcloud_rate_limit_burst,
        )
        self._semaphore = semaphore or asyncio.Semaphore(1)
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    async def search(self, query: str, limit: int) -> list[Candidate]:
        if not self.configured:
            raise ProviderNotConfigured(
                "SOUNDCLOUD_CLIENT_ID and SOUNDCLOUD_CLIENT_SECRET are not configured"
            )
        if not await self._limiter.acquire(
            self._settings.provider_rate_limit_wait_seconds
        ):
            raise ProviderRateLimited("Local SoundCloud rate limit was reached")

        result_count = max(1, min(limit, self._max_results, 50))
        async with self._semaphore:
            token = await self._access_token()
            try:
                data = await self._search_request(token, query, result_count)
            except HttpStatusError as exc:
                if exc.status_code != 401:
                    raise _classify_status_error(exc) from exc
                await self._invalidate_token(token)
                token = await self._access_token(force=True)
                try:
                    data = await self._search_request(token, query, result_count)
                except HttpStatusError as retry_exc:
                    raise _classify_status_error(retry_exc) from retry_exc
                except HttpTimeoutError as retry_exc:
                    raise ProviderTimeout("SoundCloud request timed out") from retry_exc
                except (
                    HttpNetworkError,
                    HttpDecodeError,
                    HttpResponseTooLarge,
                    HttpRedirectError,
                ) as retry_exc:
                    raise ProviderUpstreamError(
                        "SoundCloud request failed"
                    ) from retry_exc
            except HttpTimeoutError as exc:
                raise ProviderTimeout("SoundCloud request timed out") from exc
            except (
                HttpNetworkError,
                HttpDecodeError,
                HttpResponseTooLarge,
                HttpRedirectError,
            ) as exc:
                raise ProviderUpstreamError("SoundCloud request failed") from exc

        return _parse_tracks(data)

    async def _search_request(self, token: str, query: str, limit: int) -> Any:
        return await self._http.get_json(
            f"{SOUNDCLOUD_API_BASE}/tracks",
            max_bytes=self._max_bytes,
            timeout=self._timeout,
            allowed_hosts=frozenset({SOUNDCLOUD_API_HOST}),
            headers={
                "Authorization": f"OAuth {token}",
                "Accept": "application/json; charset=utf-8",
            },
            params={
                "q": query,
                "access": "playable",
                "limit": str(limit),
                "linked_partitioning": "true",
            },
        )

    async def _access_token(self, force: bool = False) -> str:
        async with self._token_lock:
            now = float(self._clock())
            if not force and self._token and self._token_expires_at - 60.0 > now:
                return self._token

            try:
                data = await self._http.post_form_json(
                    SOUNDCLOUD_TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=(self._client_id or "", self._client_secret or ""),
                    max_bytes=min(self._max_bytes, 65_536),
                    timeout=self._timeout,
                    allowed_hosts=frozenset({SOUNDCLOUD_TOKEN_HOST}),
                    headers={"Accept": "application/json; charset=utf-8"},
                )
            except HttpStatusError as exc:
                raise _classify_token_status_error(exc) from exc
            except HttpTimeoutError as exc:
                raise ProviderTimeout("SoundCloud token request timed out") from exc
            except (
                HttpNetworkError,
                HttpDecodeError,
                HttpResponseTooLarge,
                HttpRedirectError,
            ) as exc:
                raise ProviderUpstreamError("SoundCloud token request failed") from exc

            if not isinstance(data, dict):
                raise ProviderAuthError("SoundCloud token response was malformed")
            token = data.get("access_token")
            if not isinstance(token, str) or not token:
                raise ProviderAuthError("SoundCloud did not return an access token")
            try:
                expires_in = float(data.get("expires_in", 3600))
            except (TypeError, ValueError):
                expires_in = 3600.0
            self._token = token
            self._token_expires_at = now + max(0.0, expires_in)
            return token

    async def _invalidate_token(self, token: str) -> None:
        async with self._token_lock:
            if self._token == token:
                self._token = None
                self._token_expires_at = 0.0


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _parse_tracks(data: Any) -> list[Candidate]:
    if isinstance(data, dict):
        items = data.get("collection")
    else:
        items = data
    if not isinstance(items, list):
        raise ProviderUpstreamError("SoundCloud search returned an unexpected response")

    candidates: list[Candidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind != "track":
            continue
        if item.get("access") not in (None, "playable"):
            continue
        sharing = item.get("sharing")
        if sharing is not None and sharing != "public":
            continue

        raw_title = item.get("title")
        if not isinstance(raw_title, str):
            continue
        title = html.unescape(raw_title).strip()
        duration = item.get("duration")
        if not _finite_number(duration) or duration <= 0:
            continue
        permalink = _canonical_soundcloud_url(item.get("permalink_url"))
        if permalink is None:
            continue

        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        uploader = None
        if isinstance(user, dict):
            for key in ("username", "full_name"):
                value = user.get(key)
                if isinstance(value, str) and value.strip():
                    uploader = value.strip()
                    break
        metadata_artist = item.get("metadata_artist")
        artist = (
            metadata_artist.strip()
            if isinstance(metadata_artist, str) and metadata_artist.strip()
            else uploader
        )
        if not artist:
            continue
        user_id = user.get("id") if isinstance(user, dict) else None
        channel_id = (
            str(user_id)
            if isinstance(user_id, (int, str))
            and not isinstance(user_id, bool)
            and user_id not in ("", 0)
            else None
        )

        candidates.append(
            Candidate(
                platform="soundcloud",
                url=permalink,
                title=title,
                artist=artist,
                uploader=uploader,
                duration_ms=int(duration),
                channel_id=channel_id,
                official_hint=user.get("verified") is True
                if isinstance(user, dict)
                else False,
            )
        )
    return candidates


def _canonical_soundcloud_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if port is not None and port not in {80, 443}:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}:
        return None
    path = parsed.path.rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) != 2 or any(segment in {".", ".."} for segment in segments):
        return None
    if segments[0].lower() in _RESERVED_FIRST_SEGMENTS:
        return None
    if segments[1].lower() in _RESERVED_SECOND_SEGMENTS:
        return None
    if not _TRACK_PATH_RE.fullmatch(path):
        return None
    return urlunsplit(("https", "soundcloud.com", path, "", ""))


def _classify_status_error(exc: HttpStatusError) -> Exception:
    if exc.status_code == 429:
        return ProviderRateLimited("SoundCloud rate limit exceeded")
    if exc.status_code in {401, 403}:
        return ProviderAuthError("SoundCloud rejected its API credentials")
    if exc.status_code >= 500:
        return ProviderUpstreamError("SoundCloud service was unavailable")
    return ProviderUpstreamError("SoundCloud API request failed")


def _classify_token_status_error(exc: HttpStatusError) -> Exception:
    if exc.status_code == 429:
        return ProviderRateLimited("SoundCloud token endpoint rate limit exceeded")
    if exc.status_code in {400, 401, 403}:
        return ProviderAuthError("SoundCloud rejected its API credentials")
    if exc.status_code >= 500:
        return ProviderUpstreamError("SoundCloud token endpoint was unavailable")
    return ProviderUpstreamError("SoundCloud token request failed")
