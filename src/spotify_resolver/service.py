from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping

from .cache import Cache
from .circuit import CircuitBreaker
from .concurrency import KeyedLocks
from .config import Settings
from .errors import (
    InvalidSpotifyUrl,
    ProviderError,
    ResolverError,
    SpotifyMetadataError,
    SpotifyNotFound,
    SpotifyRateLimited,
    SpotifyTimeout,
    SpotifyUnavailable,
)
from .matching import (
    ScoringConfig,
    best_accepted,
    build_queries,
    rank_candidates,
    score_candidate,
    search_fallback_url,
)
from .models import (
    PLATFORMS,
    Attempt,
    CachedResolution,
    Candidate,
    ResolutionResult,
    ScoredCandidate,
    SearchFallback,
    SpotifyTrack,
)
from .providers.base import Provider
from .providers.ytmusic import YTMusicMatcher
from .spotify import SpotifyClient, parse_spotify_track_url

logger = logging.getLogger(__name__)

_STALE_FALLBACK_STATUSES = frozenset(
    {"rate_limited", "timeout", "auth_error", "upstream_error"}
)
# Primary-provider failures that open the circuit breaker.
_BREAKER_STATUSES = frozenset({"rate_limited", "auth_error"})


class ResolverService:
    def __init__(
        self,
        settings: Settings,
        spotify: SpotifyClient,
        cache: Cache,
        providers: Mapping[str, Provider],
        *,
        clock: Callable[[], float] = time.time,
        youtube_fallback: YTMusicMatcher | None = None,
        primary_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings
        self._spotify = spotify
        self._cache = cache
        self._providers = dict(providers)
        self._scoring = ScoringConfig.from_settings(settings)
        self._clock = clock
        self._metadata_locks = KeyedLocks()
        self._resolution_locks = KeyedLocks()
        self._youtube_fallback = (
            youtube_fallback
            if youtube_fallback is not None and youtube_fallback.configured
            else None
        )
        self._primary_breaker = primary_breaker

    def platform_available(self, platform: str) -> bool:
        provider = self._providers.get(platform)
        if provider is not None and provider.configured:
            return True
        return platform == "youtube" and self._youtube_fallback is not None

    async def resolve(
        self,
        raw_url: str,
        preferred_platform: str,
        allow_fallback: bool,
    ) -> ResolutionResult:
        preferred_platform = preferred_platform.strip().lower()
        if preferred_platform not in PLATFORMS:
            raise ResolverError(
                "invalid_platform",
                "platform must be youtube or soundcloud",
                status_code=400,
            )
        try:
            track_id = parse_spotify_track_url(raw_url)
        except InvalidSpotifyUrl as exc:
            raise ResolverError(
                "unsupported_spotify_url",
                str(exc),
                status_code=422,
            ) from exc

        if not any(self.platform_available(platform) for platform in PLATFORMS):
            raise ResolverError(
                "providers_not_configured",
                "No provider is configured. Set YOUTUBE_API_KEY or SoundCloud credentials.",
                status_code=503,
            )

        fallback_platform = _other_platform(preferred_platform)
        if not self.platform_available(preferred_platform) and not allow_fallback:
            raise ResolverError(
                "provider_not_configured",
                f"The requested platform ({preferred_platform}) is not configured and fallback is disabled.",
                status_code=503,
            )

        attempts: list[Attempt] = []
        metadata_cached = False
        cached_negative: dict[str, CachedResolution] = {}
        track: SpotifyTrack | None = None

        metadata_entry = await self._cache_get_metadata(track_id)
        if metadata_entry is not None:
            status, cached_track = metadata_entry
            if status == "not_found":
                raise ResolverError(
                    "spotify_track_not_found",
                    "Spotify reports that this track does not exist.",
                    status_code=404,
                )
            track = cached_track
            metadata_cached = True

        if track is None:
            preferred_cache = await self._cache_get_resolution(
                track_id, preferred_platform
            )
            if preferred_cache is not None and preferred_cache.status == "matched":
                cached_result = self._cached_match_result(
                    preferred_cache,
                    [
                        Attempt(
                            preferred_platform,
                            "verified",
                            cached=True,
                            best_confidence=_cached_confidence(preferred_cache),
                        )
                    ],
                    metadata_cached=True,
                )
                if cached_result is not None:
                    return cached_result
                preferred_cache = None

            preferred_negative = False
            if preferred_cache is not None and preferred_cache.status == "not_found":
                track = preferred_cache.track
                metadata_cached = True
                cached_negative[preferred_platform] = preferred_cache
                preferred_negative = True

            preferred_unavailable = not self.platform_available(preferred_platform)
            if allow_fallback and (preferred_negative or preferred_unavailable):
                fallback_cache = await self._cache_get_resolution(
                    track_id, fallback_platform
                )
                if fallback_cache is not None and fallback_cache.status == "matched":
                    if preferred_negative:
                        preferred_attempt = Attempt(
                            preferred_platform,
                            "no_match",
                            cached=True,
                            best_confidence=(
                                _cached_confidence(preferred_cache)
                                if preferred_cache is not None
                                else None
                            ),
                        )
                    else:
                        preferred_attempt = Attempt(
                            preferred_platform, "not_configured"
                        )
                    cached_attempts = [
                        preferred_attempt,
                        Attempt(
                            fallback_platform,
                            "verified",
                            cached=True,
                            best_confidence=_cached_confidence(fallback_cache),
                        ),
                    ]
                    cached_result = self._cached_match_result(
                        fallback_cache,
                        cached_attempts,
                        metadata_cached=True,
                    )
                    if cached_result is not None:
                        return cached_result
                if fallback_cache is not None and fallback_cache.status == "not_found":
                    if track is None:
                        track = fallback_cache.track
                        metadata_cached = True
                    cached_negative[fallback_platform] = fallback_cache

        if track is None:
            track, fetched_from_cache = await self._get_metadata(track_id)
            metadata_cached = metadata_cached or fetched_from_cache

        platforms = [preferred_platform]
        if allow_fallback:
            platforms.append(fallback_platform)

        for platform in platforms:
            cached = cached_negative.get(platform)
            if cached is not None:
                attempts.append(
                    Attempt(
                        platform,
                        "no_match",
                        cached=True,
                        best_confidence=cached.best_confidence,
                    )
                )
                continue

            if not self.platform_available(platform):
                attempts.append(Attempt(platform, "not_configured"))
                continue

            match, match_cached = await self._resolve_platform(
                track, platform, attempts
            )
            if match is not None:
                return ResolutionResult(
                    track=track,
                    match=match,
                    search_fallback=None,
                    cached=match_cached,
                    metadata_cached=metadata_cached,
                    attempts=tuple(attempts),
                    stale=any(attempt.status == "stale" for attempt in attempts),
                )

        completed = [attempt for attempt in attempts if attempt.status == "no_match"]
        fallback = SearchFallback(
            platform=preferred_platform,
            url=search_fallback_url(preferred_platform, track),
            verified=False,
            reason=("no_verified_match" if completed else "provider_unavailable"),
        )
        if not completed:
            raise self._provider_failure_error(
                track,
                attempts,
                fallback,
                metadata_cached=metadata_cached,
            )

        cached_negative_results = [attempt for attempt in completed if attempt.cached]
        return ResolutionResult(
            track=track,
            match=None,
            search_fallback=fallback,
            cached=len(cached_negative_results) == len(completed),
            metadata_cached=metadata_cached,
            attempts=tuple(attempts),
        )

    async def _resolve_platform(
        self,
        track: SpotifyTrack,
        platform: str,
        attempts: list[Attempt],
    ) -> tuple[ScoredCandidate | None, bool]:
        async with self._resolution_locks.lock(
            f"resolution:{track.spotify_id}:{platform}"
        ):
            cached = await self._cache_get_resolution(track.spotify_id, platform)
            if cached is not None:
                if cached.status == "matched" and cached.candidate is not None:
                    validated = self._validated_cached_match(cached)
                    if validated is not None:
                        attempts.append(
                            Attempt(
                                platform,
                                "verified",
                                cached=True,
                                best_confidence=validated.score.total,
                            )
                        )
                        return validated, True
                if cached.status == "not_found":
                    attempts.append(
                        Attempt(
                            platform,
                            "no_match",
                            cached=True,
                            best_confidence=cached.best_confidence,
                        )
                    )
                    return None, True

            if not self.platform_available(platform):
                attempts.append(Attempt(platform, "not_configured"))
                return None, False

            accepted, best_confidence, provider_error, searched = await self._search(
                track, platform
            )
            if accepted is not None:
                now = float(self._clock())
                await self._cache_put_resolution(
                    track,
                    platform,
                    "matched",
                    candidate=accepted,
                    best_confidence=accepted.score.total,
                    now=now,
                    ttl_seconds=self._settings.match_cache_ttl_seconds,
                )
                attempts.append(
                    Attempt(
                        platform,
                        "verified",
                        cached=False,
                        best_confidence=accepted.score.total,
                    )
                )
                return accepted, False

            if provider_error is not None:
                if provider_error.status in _STALE_FALLBACK_STATUSES:
                    stale = await self._cache_get_stale_resolution(
                        track.spotify_id,
                        platform,
                    )
                    if stale is not None:
                        validated = self._validated_cached_match(stale)
                        if validated is not None:
                            attempts.append(
                                Attempt(
                                    platform,
                                    "stale",
                                    cached=True,
                                    best_confidence=validated.score.total,
                                )
                            )
                            logger.info(
                                "Serving stale %s match for %s after provider error: %s",
                                platform,
                                track.spotify_id,
                                provider_error.status,
                            )
                            return validated, True
                attempts.append(
                    Attempt(
                        platform,
                        provider_error.status,
                        cached=False,
                        best_confidence=best_confidence,
                    )
                )
                return None, False

            if searched:
                await self._cache_put_resolution(
                    track,
                    platform,
                    "not_found",
                    candidate=None,
                    best_confidence=best_confidence,
                    now=float(self._clock()),
                    ttl_seconds=self._settings.negative_cache_ttl_seconds,
                )
            attempts.append(
                Attempt(
                    platform,
                    "no_match",
                    cached=False,
                    best_confidence=best_confidence,
                )
            )
            return None, False

    async def _search(
        self, track: SpotifyTrack, platform: str
    ) -> tuple[ScoredCandidate | None, float | None, ProviderError | None, bool]:
        """Search the platform's provider chain.

        Returns (accepted, best_confidence, provider_error, searched). For
        YouTube the chain is the YouTube Data API (guarded by a circuit
        breaker) followed by the ytmusicapi fallback when the primary fails,
        is skipped, or finds no acceptable match. `searched` is True only when
        every provider that should have been asked completed its search, so a
        negative result can safely be cached.
        """
        provider = self._providers.get(platform)
        primary_configured = provider is not None and provider.configured
        fallback = self._youtube_fallback if platform == "youtube" else None
        breaker = self._primary_breaker if platform == "youtube" else None

        accepted: ScoredCandidate | None = None
        best_confidence: float | None = None
        primary_error: ProviderError | None = None
        primary_completed = False

        if primary_configured and (breaker is None or breaker.allow()):
            assert provider is not None
            accepted, best_confidence, primary_error = await self._search_provider(
                track, platform, provider
            )
            primary_completed = primary_error is None
            if breaker is not None:
                self._update_breaker(breaker, platform, primary_error)
        elif primary_configured:
            logger.info("Primary %s resolver skipped: circuit open", platform)

        if accepted is not None or fallback is None:
            return accepted, best_confidence, primary_error, primary_completed

        outcome = await fallback.match(track, scoring=self._scoring)
        if outcome.best_confidence is not None:
            best_confidence = max(best_confidence or 0.0, outcome.best_confidence)
        if outcome.status == "matched":
            return outcome.match, best_confidence, None, True
        fallback_completed = outcome.error is None
        if primary_completed or fallback_completed:
            # At least one provider finished searching without a match. Cache
            # the negative result only if no provider was skipped or failed.
            primary_done = primary_completed or not primary_configured
            return None, best_confidence, None, primary_done and fallback_completed
        return None, best_confidence, primary_error or outcome.error, False

    def _update_breaker(
        self,
        breaker: CircuitBreaker,
        platform: str,
        error: ProviderError | None,
    ) -> None:
        if error is None:
            breaker.record_success()
            return
        tripping = error.status == "auth_error" or (
            error.status == "rate_limited" and getattr(error, "upstream", False)
        )
        if tripping:
            breaker.record_failure(
                error.status, getattr(error, "retry_after_seconds", None)
            )
            logger.warning(
                "Primary %s resolver unavailable (%s); preferring fallback",
                platform,
                error.status,
            )
        else:
            breaker.release()

    async def _search_provider(
        self, track: SpotifyTrack, platform: str, provider: Provider
    ) -> tuple[ScoredCandidate | None, float | None, ProviderError | None]:
        max_queries = (
            self._settings.soundcloud_max_queries
            if platform == "soundcloud"
            else self._settings.youtube_max_queries
        )
        limit = (
            self._settings.soundcloud_max_results
            if platform == "soundcloud"
            else self._settings.youtube_max_results
        )
        queries = build_queries(track, platform, max_queries)
        raw_candidates: dict[str, Candidate] = {}
        ranked: list[ScoredCandidate] = []
        best_confidence: float | None = None
        try:
            for index, query in enumerate(queries):
                candidates = await provider.search(query, limit)
                for candidate in candidates:
                    raw_candidates[candidate.url] = candidate
                ranked = rank_candidates(
                    track,
                    list(raw_candidates.values()),
                    platform,
                    self._scoring,
                )
                if ranked:
                    best_confidence = ranked[0].score.total
                accepted = best_accepted(ranked, self._scoring)
                if accepted is not None:
                    remaining = index + 1 < len(queries)
                    strong_enough = (
                        accepted.score.total
                        >= self._scoring.threshold
                        + self._settings.search_extra_query_margin
                    )
                    if not remaining or strong_enough:
                        break
        except ProviderError as exc:
            return best_accepted(ranked, self._scoring), best_confidence, exc
        return best_accepted(ranked, self._scoring), best_confidence, None

    async def _get_metadata(self, track_id: str) -> tuple[SpotifyTrack, bool]:
        async with self._metadata_locks.lock(f"metadata:{track_id}"):
            entry = await self._cache_get_metadata(track_id)
            if entry is not None:
                status, track = entry
                if status == "not_found":
                    raise ResolverError(
                        "spotify_track_not_found",
                        "Spotify reports that this track does not exist.",
                        status_code=404,
                    )
                if track is not None:
                    return track, True

            try:
                track = await self._spotify.get_track(track_id)
            except SpotifyNotFound as exc:
                await self._cache_put_metadata_not_found(track_id)
                raise ResolverError(
                    "spotify_track_not_found",
                    "Spotify reports that this track does not exist.",
                    status_code=404,
                ) from exc
            except SpotifyTimeout as exc:
                raise ResolverError(
                    "spotify_timeout",
                    "The Spotify page request timed out.",
                    status_code=504,
                    retryable=True,
                ) from exc
            except SpotifyRateLimited as exc:
                raise ResolverError(
                    "spotify_rate_limited",
                    "Spotify rate-limited the public page request.",
                    status_code=503,
                    retryable=True,
                    retry_after_seconds=exc.retry_after_seconds,
                ) from exc
            except SpotifyUnavailable as exc:
                raise ResolverError(
                    "spotify_unavailable",
                    "Spotify was unavailable while fetching track metadata.",
                    status_code=503,
                    retryable=True,
                ) from exc
            except SpotifyMetadataError as exc:
                raise ResolverError(
                    "spotify_metadata_unavailable",
                    "Spotify did not expose usable metadata for this track.",
                    status_code=502,
                ) from exc

            await self._cache_put_metadata(track)
            return track, False

    def _provider_failure_error(
        self,
        track: SpotifyTrack,
        attempts: list[Attempt],
        fallback: SearchFallback,
        *,
        metadata_cached: bool,
    ) -> ResolverError:
        statuses = {attempt.status for attempt in attempts}
        if statuses and statuses <= {"timeout"}:
            code = "providers_timeout"
            message = "Every enabled provider timed out."
            status_code = 504
        elif "rate_limited" in statuses:
            code = "providers_rate_limited"
            message = "Every enabled provider was rate-limited."
            status_code = 503
        elif "auth_error" in statuses:
            code = "providers_auth_error"
            message = "A provider rejected its credentials."
            status_code = 503
        elif statuses <= {"not_configured"}:
            code = "provider_not_configured"
            message = "The requested provider is not configured."
            status_code = 503
        else:
            code = "providers_unavailable"
            message = "No enabled provider could complete the search."
            status_code = 503
        return ResolverError(
            code,
            message,
            status_code=status_code,
            retryable=code != "provider_not_configured",
            track=track,
            attempts=tuple(attempts),
            search_fallback=fallback,
            metadata_cached=metadata_cached,
        )

    def _cached_match_result(
        self,
        cached: CachedResolution,
        attempts: list[Attempt],
        *,
        metadata_cached: bool,
    ) -> ResolutionResult | None:
        rescored = self._validated_cached_match(cached)
        if rescored is None:
            return None
        updated_attempts = tuple(
            Attempt(
                attempt.platform,
                attempt.status,
                cached=attempt.cached,
                best_confidence=(
                    rescored.score.total
                    if attempt.platform == cached.platform
                    and attempt.status == "verified"
                    else attempt.best_confidence
                ),
            )
            for attempt in attempts
        )
        return ResolutionResult(
            track=cached.track,
            match=rescored,
            search_fallback=None,
            cached=True,
            metadata_cached=metadata_cached,
            attempts=updated_attempts,
        )

    def _validated_cached_match(
        self,
        cached: CachedResolution,
    ) -> ScoredCandidate | None:
        if cached.candidate is None:
            return None
        rescored = score_candidate(
            cached.track,
            cached.candidate.candidate,
            cached.platform,
            self._scoring,
        )
        if rescored.eligible and rescored.score.total >= self._scoring.threshold:
            return rescored
        return None

    async def _cache_get_metadata(self, track_id: str):
        try:
            return await self._cache.get_metadata(track_id, float(self._clock()))
        except Exception:
            logger.warning("Metadata cache read failed", exc_info=True)
            return None

    async def _cache_get_resolution(self, track_id: str, platform: str):
        try:
            return await self._cache.get_resolution(
                track_id,
                platform,
                float(self._clock()),
            )
        except Exception:
            logger.warning("Resolution cache read failed", exc_info=True)
            return None

    async def _cache_get_stale_resolution(self, track_id: str, platform: str):
        try:
            return await self._cache.get_stale_resolution(
                track_id,
                platform,
                float(self._clock()),
                self._settings.stale_match_grace_seconds,
            )
        except Exception:
            logger.warning("Stale resolution cache read failed", exc_info=True)
            return None

    async def _cache_put_metadata(self, track: SpotifyTrack) -> None:
        try:
            await self._cache.put_metadata(
                track,
                float(self._clock()),
                self._settings.metadata_cache_ttl_seconds,
            )
        except Exception:
            logger.warning("Metadata cache write failed", exc_info=True)

    async def _cache_put_metadata_not_found(self, track_id: str) -> None:
        try:
            await self._cache.put_metadata_not_found(
                track_id,
                float(self._clock()),
                self._settings.spotify_not_found_cache_ttl_seconds,
            )
        except Exception:
            logger.warning("Metadata negative-cache write failed", exc_info=True)

    async def _cache_put_resolution(
        self,
        track: SpotifyTrack,
        platform: str,
        status: str,
        *,
        candidate: ScoredCandidate | None,
        best_confidence: float | None,
        now: float,
        ttl_seconds: int,
    ) -> None:
        try:
            await self._cache.put_resolution(
                track,
                platform,
                status,
                candidate=candidate,
                best_confidence=best_confidence,
                now=now,
                ttl_seconds=ttl_seconds,
            )
        except Exception:
            logger.warning("Resolution cache write failed", exc_info=True)


def _other_platform(platform: str) -> str:
    return "soundcloud" if platform == "youtube" else "youtube"


def _cached_confidence(cached: CachedResolution) -> float | None:
    if cached.candidate is not None:
        return cached.candidate.score.total
    return cached.confidence
