from __future__ import annotations

import asyncio
import html
import json
import math
import re
from typing import Any

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

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_HOST = "www.googleapis.com"

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_QUOTA_REASONS = {
    "quotaExceeded",
    "dailyLimitExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}
_AUTH_REASONS = {
    "accessNotConfigured",
    "forbidden",
    "ipRefererBlocked",
    "keyInvalid",
    "requestDenied",
}


class YouTubeProvider(Provider):
    name = "youtube"

    def __init__(
        self,
        http: LimitedHttpClient,
        settings: Settings,
        *,
        limiter: AsyncRateLimiter | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._http = http
        self._settings = settings
        self._api_key = settings.youtube_api_key
        self._timeout = settings.provider_timeout_seconds
        self._max_bytes = settings.provider_max_bytes
        self._max_results = min(50, settings.youtube_max_results)
        self._limiter = limiter or AsyncRateLimiter(
            settings.youtube_rate_limit_per_minute,
            burst=settings.youtube_rate_limit_burst,
        )
        self._semaphore = semaphore or asyncio.Semaphore(1)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, limit: int) -> list[Candidate]:
        if not self._api_key:
            raise ProviderNotConfigured("YOUTUBE_API_KEY is not configured")

        if not await self._limiter.acquire(
            self._settings.provider_rate_limit_wait_seconds
        ):
            raise ProviderRateLimited("Local YouTube rate limit was reached")

        result_count = max(1, min(limit, self._max_results, 50))
        async with self._semaphore:
            try:
                search_data = await self._http.get_json(
                    YOUTUBE_SEARCH_URL,
                    max_bytes=self._max_bytes,
                    timeout=self._timeout,
                    allowed_hosts=frozenset({YOUTUBE_HOST}),
                    params={
                        "key": self._api_key,
                        "part": "snippet",
                        "q": query,
                        "type": "video",
                        "maxResults": str(result_count),
                        "safeSearch": "none",
                        "fields": (
                            "items(id/videoId,"
                            "snippet(title,channelTitle,channelId,liveBroadcastContent))"
                        ),
                    },
                )
                video_ids = _extract_video_ids(search_data)
                if not video_ids:
                    return []

                details_data = await self._http.get_json(
                    YOUTUBE_VIDEOS_URL,
                    max_bytes=self._max_bytes,
                    timeout=self._timeout,
                    allowed_hosts=frozenset({YOUTUBE_HOST}),
                    params={
                        "key": self._api_key,
                        "part": "snippet,contentDetails,status",
                        "id": ",".join(video_ids),
                        "maxResults": str(len(video_ids)),
                        "fields": (
                            "items(id,"
                            "snippet(title,channelTitle,channelId,liveBroadcastContent),"
                            "contentDetails(duration),"
                            "status(uploadStatus,privacyStatus,embeddable))"
                        ),
                    },
                )
            except HttpStatusError as exc:
                raise _classify_status_error(exc) from exc
            except HttpTimeoutError as exc:
                raise ProviderTimeout("YouTube request timed out") from exc
            except (
                HttpNetworkError,
                HttpDecodeError,
                HttpResponseTooLarge,
                HttpRedirectError,
            ) as exc:
                raise ProviderUpstreamError("YouTube request failed") from exc

        return _parse_video_details(details_data, video_ids)


def _extract_video_ids(data: Any) -> list[str]:
    if not isinstance(data, dict):
        raise ProviderUpstreamError("YouTube search returned an unexpected response")
    items = data.get("items", [])
    if not isinstance(items, list):
        raise ProviderUpstreamError("YouTube search returned an unexpected response")
    ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if not isinstance(identifier, dict):
            continue
        video_id = identifier.get("videoId")
        if (
            isinstance(video_id, str)
            and _VIDEO_ID_RE.fullmatch(video_id)
            and video_id not in seen
        ):
            seen.add(video_id)
            ids.append(video_id)
    return ids


def _parse_video_details(data: Any, video_ids: list[str]) -> list[Candidate]:
    if not isinstance(data, dict):
        raise ProviderUpstreamError(
            "YouTube video details returned an unexpected response"
        )
    items = data.get("items", [])
    if not isinstance(items, list):
        raise ProviderUpstreamError(
            "YouTube video details returned an unexpected response"
        )

    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id[item["id"]] = item

    candidates: list[Candidate] = []
    for video_id in video_ids:
        item = by_id.get(video_id)
        if not item:
            continue
        snippet = item.get("snippet")
        content = item.get("contentDetails")
        status = item.get("status")
        if not isinstance(snippet, dict) or not isinstance(content, dict):
            continue
        if isinstance(status, dict):
            if status.get("privacyStatus", "public") != "public":
                continue
            if status.get("uploadStatus", "processed") != "processed":
                continue
        if snippet.get("liveBroadcastContent") not in (None, "none"):
            continue

        duration_ms = parse_iso8601_duration(content.get("duration"))
        if duration_ms is None:
            continue
        raw_title = snippet.get("title")
        raw_channel = snippet.get("channelTitle")
        if not isinstance(raw_title, str) or not isinstance(raw_channel, str):
            continue
        title = html.unescape(raw_title).strip()
        channel = html.unescape(raw_channel).strip()
        if not title or not channel:
            continue
        channel_lower = channel.casefold()
        channel_id = snippet.get("channelId")
        candidates.append(
            Candidate(
                platform="youtube",
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=title,
                artist=channel,
                uploader=channel,
                duration_ms=duration_ms,
                channel_id=(
                    channel_id.strip()
                    if isinstance(channel_id, str) and channel_id.strip()
                    else None
                ),
                is_topic=bool(re.search(r"\s*-\s*topic\s*$", channel_lower)),
                is_vevo=channel_lower.endswith("vevo"),
                official_hint=bool(re.search(r"\bofficial\b", channel_lower)),
                source="youtube_data_api",
            )
        )
    return candidates


def parse_iso8601_duration(value: Any) -> int | None:
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


def _classify_status_error(exc: HttpStatusError) -> Exception:
    if exc.status_code == 429:
        return ProviderRateLimited(
            "YouTube rate limit exceeded",
            upstream=True,
            retry_after_seconds=exc.retry_after,
        )
    if exc.status_code in {400, 403}:
        reasons = _youtube_error_reasons(exc.body)
        if reasons & _QUOTA_REASONS:
            return ProviderRateLimited(
                "YouTube API quota or rate limit exceeded",
                upstream=True,
                retry_after_seconds=exc.retry_after,
            )
        if reasons & _AUTH_REASONS:
            return ProviderAuthError("YouTube API key was rejected")
        return ProviderUpstreamError("YouTube API request was rejected")
    if exc.status_code >= 500:
        return ProviderUpstreamError("YouTube service was unavailable")
    return ProviderUpstreamError("YouTube API request failed")


def _youtube_error_reasons(body: bytes) -> set[str]:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return set()
    reasons: set[str] = set()
    error = data.get("error") if isinstance(data, dict) else None
    errors = error.get("errors") if isinstance(error, dict) else None
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"])
    return reasons
