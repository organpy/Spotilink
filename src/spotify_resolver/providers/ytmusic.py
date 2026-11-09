"""YouTube Music search through ytmusicapi.

ytmusicapi is synchronous (requests-based), so calls run in worker threads
behind a semaphore (YT_MUSIC_CONCURRENCY) shared by every caller, which bounds
both thread usage and the request rate against YouTube Music. Each worker
thread owns its own unauthenticated YTMusic instance and HTTP session with a
request timeout.

YouTube Music can throttle too: HTTP 429/403 responses are mapped to
ProviderRateLimited so callers back off instead of hammering it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from ..config import Settings
from ..errors import (
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUpstreamError,
)
from ..matching import (
    ScoringConfig,
    best_accepted,
    clean_title,
    rank_candidates,
)
from ..models import Candidate, ScoredCandidate, SpotifyTrack
from ..urls import is_valid_video_id

logger = logging.getLogger(__name__)

PROVIDER_NAME = "ytmusicapi"
_LYRIC_UPLOAD_PENALTY = 0.05


def _default_factory(timeout: float) -> Any:
    import requests
    from ytmusicapi import YTMusic

    session = requests.Session()
    session.request = functools.partial(session.request, timeout=timeout)  # type: ignore[method-assign]
    return YTMusic(requests_session=session)


class YTMusicClient:
    def __init__(
        self,
        settings: Settings,
        *,
        factory: Callable[[], Any] | None = None,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._settings = settings
        self._timeout = settings.yt_music_timeout_seconds
        self._factory = factory or (lambda: _default_factory(self._timeout))
        self._local = threading.local()
        self._semaphore = semaphore or asyncio.Semaphore(settings.yt_music_concurrency)

    @property
    def configured(self) -> bool:
        return self._settings.yt_music_enabled

    async def search(self, query: str, kind: str, limit: int) -> list[Candidate]:
        if kind not in {"songs", "videos"}:
            raise ValueError("kind must be songs or videos")
        async with self._semaphore:
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(self._search_sync, query, kind, limit),
                    timeout=self._timeout * 2 + 5,
                )
            except TimeoutError as exc:
                raise ProviderTimeout("YouTube Music search timed out") from exc
        return parse_results(raw, kind)

    def _search_sync(self, query: str, kind: str, limit: int) -> Any:
        client = getattr(self._local, "client", None)
        try:
            if client is None:
                client = self._factory()
                self._local.client = client
            return client.search(query, filter=kind, limit=limit)
        except Exception as exc:  # ytmusicapi raises many unrelated types
            raise classify_exception(exc) from None


def classify_exception(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    name = type(exc).__name__
    text = str(exc)
    if "Timeout" in name:
        return ProviderTimeout("YouTube Music request timed out")
    if "HTTP 429" in text or "HTTP 403" in text:
        return ProviderRateLimited("YouTube Music throttled the request", upstream=True)
    if "JSONDecodeError" in name:
        # YouTube answered with an empty/non-JSON body: soft throttling.
        return ProviderRateLimited("YouTube Music returned a non-JSON response", upstream=True)
    if "ConnectionError" in name or "HTTP 5" in text:
        return ProviderUpstreamError("YouTube Music was unavailable")
    logger.warning("Unexpected ytmusicapi failure: %s", name)
    return ProviderUpstreamError("YouTube Music returned an unexpected response")


def parse_results(raw: Any, kind: str) -> list[Candidate]:
    if not isinstance(raw, list):
        raise ProviderUpstreamError("YouTube Music returned an unexpected response")
    candidates: list[Candidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        result_type = item.get("resultType")
        if result_type not in {"song", "video"}:
            continue
        video_id = item.get("videoId")
        title = item.get("title")
        if not is_valid_video_id(video_id) or not isinstance(title, str) or not title.strip():
            continue
        artists = [
            a["name"].strip()
            for a in item.get("artists") or []
            if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"].strip()
        ]
        seconds = item.get("duration_seconds")
        duration_ms = (
            int(seconds) * 1000
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0
            else None
        )
        video_type = item.get("videoType")
        candidates.append(
            Candidate(
                platform="youtube",
                url=f"https://www.youtube.com/watch?v={video_id}",
                title=title.strip(),
                artist=", ".join(artists) or None,
                uploader=artists[0] if artists else None,
                duration_ms=duration_ms,
                # Song results are art tracks published by the artist's
                # "- Topic" channel; OMV is an official music video.
                is_topic=result_type == "song" or video_type == "MUSIC_VIDEO_TYPE_ATV",
                official_hint=video_type == "MUSIC_VIDEO_TYPE_OMV",
                extra={"result_type": result_type, "video_type": video_type},
                source=PROVIDER_NAME,
            )
        )
    return candidates


def build_query(track: SpotifyTrack) -> str:
    title = clean_title(track.title).replace('"', " ")
    artist = track.artists[0] if track.artists else ""
    return " ".join(f"{artist} {title}".split())[:200]


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    status: str  # matched | uncertain | unmatched | failed
    match: ScoredCandidate | None = None
    best: ScoredCandidate | None = None
    error: ProviderError | None = None
    searches: int = 0

    @property
    def best_confidence(self) -> float | None:
        best = self.match or self.best
        return best.score.total if best is not None else None


class YTMusicMatcher:
    """Match a Spotify track to a YouTube video using YouTube Music search."""

    def __init__(
        self,
        client: YTMusicClient,
        scoring: ScoringConfig,
        *,
        uncertain_threshold: float,
        max_results: int = 10,
    ) -> None:
        self._client = client
        self._scoring = scoring
        self._uncertain = uncertain_threshold
        self._limit = max_results

    @classmethod
    def from_settings(cls, client: YTMusicClient, settings: Settings) -> YTMusicMatcher:
        scoring = replace(
            ScoringConfig.from_settings(settings),
            threshold=settings.yt_music_match_threshold,
            weight_title=settings.yt_music_weight_title,
            weight_artist=settings.yt_music_weight_artist,
            weight_duration=settings.yt_music_weight_duration,
        )
        return cls(
            client,
            scoring,
            uncertain_threshold=settings.yt_music_uncertain_threshold,
            max_results=settings.yt_music_max_results,
        )

    @property
    def configured(self) -> bool:
        return self._client.configured

    @property
    def scoring(self) -> ScoringConfig:
        return self._scoring

    async def match(
        self, track: SpotifyTrack, *, scoring: ScoringConfig | None = None
    ) -> MatchOutcome:
        config = scoring or self._scoring
        query = build_query(track)
        if not query:
            return MatchOutcome("unmatched")
        pool: dict[str, Candidate] = {}
        ranked: list[ScoredCandidate] = []
        searches = 0
        error: ProviderError | None = None
        for kind in ("songs", "videos"):
            try:
                results = await self._client.search(query, kind, self._limit)
            except ProviderError as exc:
                error = exc
                break
            searches += 1
            for candidate in results:
                pool.setdefault(candidate.url, candidate)
            ranked = self.rank(track, list(pool.values()), config)
            accepted = best_accepted(ranked, config)
            if accepted is not None:
                return MatchOutcome("matched", accepted, accepted, searches=searches)
        best = ranked[0] if ranked else None
        if error is not None and best is None:
            return MatchOutcome("failed", error=error, searches=searches)
        if best is not None and best.score.total >= self._uncertain:
            return MatchOutcome("uncertain", best=best, error=error, searches=searches)
        return MatchOutcome(
            "failed" if error is not None else "unmatched",
            best=best,
            error=error,
            searches=searches,
        )

    @staticmethod
    def rank(
        track: SpotifyTrack, candidates: list[Candidate], config: ScoringConfig
    ) -> list[ScoredCandidate]:
        adjusted: list[ScoredCandidate] = []
        for scored in rank_candidates(track, candidates, "youtube", config):
            candidate = scored.candidate
            lyric_upload = (
                candidate.extra.get("result_type") == "video"
                and not candidate.official_hint
                and "lyric" in candidate.title.casefold()
                and "lyric" not in track.title.casefold()
            )
            if lyric_upload:
                score = replace(
                    scored.score,
                    total=max(0.0, scored.score.total - _LYRIC_UPLOAD_PENALTY),
                    version_penalty=scored.score.version_penalty + _LYRIC_UPLOAD_PENALTY,
                )
                scored = replace(scored, score=score)
            adjusted.append(scored)
        adjusted.sort(
            key=lambda s: (
                -round(s.score.total, 6),
                not s.candidate.is_topic,
                s.score.duration_diff_ms if s.score.duration_diff_ms is not None else 10**9,
                s.candidate.url,
            )
        )
        return adjusted
