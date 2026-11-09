"""Single-track resolution: primary YouTube resolver with ytmusicapi fallback."""

from __future__ import annotations

from conftest import (
    TRACK_TITLE,
    FakeProvider,
    FakeSpotify,
    FakeYTMusic,
    MockClock,
    ytm_candidate,
)

from spotify_resolver.cache import Cache
from spotify_resolver.circuit import CircuitBreaker
from spotify_resolver.errors import ProviderRateLimited, ProviderTimeout
from spotify_resolver.models import Candidate, SpotifyTrack
from spotify_resolver.providers.ytmusic import YTMusicMatcher
from spotify_resolver.service import ResolverService

PRIMARY_URL = "https://www.youtube.com/watch?v=AAAAAAAAAAA"
FALLBACK_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _primary_candidate() -> Candidate:
    return Candidate(
        platform="youtube",
        url=PRIMARY_URL,
        title=TRACK_TITLE,
        artist="Rick Astley - Topic",
        uploader="Rick Astley - Topic",
        duration_ms=213_573,
        is_topic=True,
        source="youtube_data_api",
    )


async def _service(settings, primary, ytm, track, *, clock=None, breaker_clock=None):
    cache = Cache(settings.database_path)
    await cache.initialize()
    breaker = CircuitBreaker("youtube_data_api", 300, clock=breaker_clock or MockClock(0.0))
    service = ResolverService(
        settings,
        FakeSpotify(track),  # type: ignore[arg-type]
        cache,
        {"youtube": primary},
        clock=clock or MockClock(),
        youtube_fallback=YTMusicMatcher.from_settings(ytm, settings) if ytm else None,
        primary_breaker=breaker,
    )
    return cache, service, breaker


async def test_primary_success_does_not_call_fallback(settings, sample_track: SpotifyTrack) -> None:
    primary = FakeProvider("youtube", [_primary_candidate()])
    ytm = FakeYTMusic(songs=[ytm_candidate()])
    cache, service, _ = await _service(settings, primary, ytm, sample_track)
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is not None and result.match.candidate.url == PRIMARY_URL
        assert ytm.calls == []
    finally:
        await cache.close()


async def test_quota_exhaustion_falls_back_and_opens_circuit(
    settings, sample_track: SpotifyTrack
) -> None:
    primary = FakeProvider(
        "youtube", error=ProviderRateLimited("quota", upstream=True, retry_after_seconds=None)
    )
    ytm = FakeYTMusic(songs=[ytm_candidate(duration_ms=213_000)])
    cache, service, breaker = await _service(settings, primary, ytm, sample_track)
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is not None
        assert result.match.candidate.url == FALLBACK_URL
        assert result.match.candidate.source == "ytmusicapi"
        assert breaker.state == "open"
        assert len(primary.calls) == 1

        # Cached match is reused; nothing is searched again.
        again = await service.resolve(sample_track.url, "youtube", False)
        assert again.cached is True
        assert len(primary.calls) == 1 and len(ytm.calls) == 1
    finally:
        await cache.close()


async def test_open_circuit_skips_primary_until_cooldown(settings) -> None:
    breaker_clock = MockClock(0.0)
    tracks = [
        SpotifyTrack(f"{i}" * 22, TRACK_TITLE, ("Rick Astley",), None, 213_573, "ms")
        for i in range(1, 4)
    ]
    primary = FakeProvider(
        "youtube",
        [_primary_candidate()],
        errors=[ProviderRateLimited("quota", upstream=True)],
    )
    ytm = FakeYTMusic(songs=[ytm_candidate(duration_ms=213_000)])
    cache = Cache(settings.database_path)
    await cache.initialize()
    breaker = CircuitBreaker("youtube_data_api", 300, clock=breaker_clock)
    spotify = FakeSpotify(tracks[0])
    service = ResolverService(
        settings,
        spotify,  # type: ignore[arg-type]
        cache,
        {"youtube": primary},
        clock=MockClock(),
        youtube_fallback=YTMusicMatcher.from_settings(ytm, settings),
        primary_breaker=breaker,
    )
    try:
        spotify.track = tracks[0]
        await service.resolve(tracks[0].url, "youtube", False)
        assert len(primary.calls) == 1 and breaker.state == "open"

        spotify.track = tracks[1]
        second = await service.resolve(tracks[1].url, "youtube", False)
        assert len(primary.calls) == 1  # skipped while open
        assert second.match is not None and second.match.candidate.source == "ytmusicapi"

        breaker_clock.advance(301)
        spotify.track = tracks[2]
        third = await service.resolve(tracks[2].url, "youtube", False)
        assert len(primary.calls) == 2  # probe after cooldown
        assert third.match is not None and third.match.candidate.url == PRIMARY_URL
        assert breaker.state == "closed"
    finally:
        await cache.close()


async def test_local_rate_limit_falls_back_without_opening_circuit(
    settings, sample_track: SpotifyTrack
) -> None:
    primary = FakeProvider("youtube", error=ProviderRateLimited("local bucket"))
    ytm = FakeYTMusic(songs=[ytm_candidate(duration_ms=213_000)])
    cache, service, breaker = await _service(settings, primary, ytm, sample_track)
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is not None and result.match.candidate.source == "ytmusicapi"
        assert breaker.state == "closed"
    finally:
        await cache.close()


async def test_primary_no_match_uses_fallback(settings, sample_track: SpotifyTrack) -> None:
    wrong = Candidate(
        platform="youtube",
        url=PRIMARY_URL,
        title="Something Else Entirely",
        artist="Other",
        uploader="Other",
        duration_ms=100_000,
    )
    primary = FakeProvider("youtube", [wrong])
    ytm = FakeYTMusic(songs=[ytm_candidate(duration_ms=213_000)])
    cache, service, breaker = await _service(settings, primary, ytm, sample_track)
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is not None and result.match.candidate.url == FALLBACK_URL
        assert breaker.state == "closed"
    finally:
        await cache.close()


async def test_both_fail_is_not_cached_as_negative(settings, sample_track: SpotifyTrack) -> None:
    primary = FakeProvider("youtube", error=ProviderRateLimited("quota", upstream=True))
    ytm = FakeYTMusic(error=ProviderTimeout())
    cache, service, _ = await _service(settings, primary, ytm, sample_track)
    try:
        try:
            await service.resolve(sample_track.url, "youtube", False)
        except Exception as exc:  # ResolverError
            assert getattr(exc, "code", "") == "providers_rate_limited"
        else:  # pragma: no cover
            raise AssertionError("expected a provider failure")
        cached = await cache.get_resolution(sample_track.spotify_id, "youtube", MockClock()())
        assert cached is None
    finally:
        await cache.close()


async def test_fallback_no_match_after_primary_error_is_not_negative_cached(
    settings, sample_track: SpotifyTrack
) -> None:
    primary = FakeProvider("youtube", error=ProviderRateLimited("quota", upstream=True))
    ytm = FakeYTMusic()
    cache, service, _ = await _service(settings, primary, ytm, sample_track)
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is None and result.search_fallback is not None
        assert await cache.get_resolution(sample_track.spotify_id, "youtube", MockClock()()) is None
    finally:
        await cache.close()


async def test_fallback_only_without_api_key(settings, sample_track: SpotifyTrack) -> None:
    primary = FakeProvider("youtube", configured=False)
    ytm = FakeYTMusic(songs=[ytm_candidate(duration_ms=213_000)])
    cache, service, _ = await _service(settings, primary, ytm, sample_track)
    try:
        assert service.platform_available("youtube")
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is not None and result.match.candidate.source == "ytmusicapi"
        assert primary.calls == []
    finally:
        await cache.close()


async def test_fallback_uses_v1_threshold(make_settings, sample_track: SpotifyTrack) -> None:
    # Scores ~0.92 under v1 weights: accepted by the 0.75 playlist threshold
    # but not by a stricter v1 MATCH_THRESHOLD.
    settings = make_settings(match_threshold=0.95)
    weak = ytm_candidate(duration_ms=235_000, kind="video")
    primary = FakeProvider("youtube", configured=False)
    cache, service, _ = await _service(settings, primary, FakeYTMusic(songs=[weak]), sample_track)
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is None
    finally:
        await cache.close()


async def test_tribute_channel_is_penalized(settings, sample_track: SpotifyTrack) -> None:
    tribute = ytm_candidate(artist="Rick Astley Tribute Band", duration_ms=213_000)
    primary = FakeProvider("youtube", configured=False)
    cache, service, _ = await _service(settings, primary, FakeYTMusic(songs=[tribute]), sample_track)
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is None
    finally:
        await cache.close()
