from __future__ import annotations

import asyncio

import pytest
from conftest import (
    TRACK_TITLE,
    FakeProvider,
    FakeSpotify,
    MockClock,
)

from spotify_resolver.cache import Cache
from spotify_resolver.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderTimeout,
    ResolverError,
    SpotifyNotFound,
    SpotifyTimeout,
)
from spotify_resolver.matching import ScoringConfig, score_candidate
from spotify_resolver.models import Candidate, SpotifyTrack
from spotify_resolver.service import ResolverService


def _candidate(
    platform: str = "youtube",
    *,
    url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    title: str = TRACK_TITLE,
    artist: str = "Rick Astley - Topic",
    duration_ms: int = 213_573,
    is_topic: bool = True,
) -> Candidate:
    return Candidate(
        platform=platform,
        url=url,
        title=title,
        artist=artist,
        uploader=artist,
        duration_ms=duration_ms,
        is_topic=is_topic,
    )


async def _service(
    settings,
    providers: dict[str, FakeProvider],
    track: SpotifyTrack,
    *,
    clock: MockClock | None = None,
    spotify: FakeSpotify | None = None,
) -> tuple[Cache, ResolverService, FakeSpotify]:
    cache = Cache(settings.database_path)
    await cache.initialize()
    fake_spotify = spotify or FakeSpotify(track)
    service = ResolverService(
        settings,
        fake_spotify,  # type: ignore[arg-type]
        cache,
        providers,
        clock=clock or MockClock(),
    )
    return cache, service, fake_spotify


@pytest.mark.asyncio
async def test_success_is_cached_and_cached_metadata_is_reported(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    provider = FakeProvider("youtube", [_candidate()])
    cache, service, spotify = await _service(
        settings, {"youtube": provider}, sample_track
    )
    try:
        first = await service.resolve(sample_track.url, "youtube", False)
        assert first.match is not None
        assert first.match.candidate.url == _candidate().url
        assert first.cached is False
        assert first.metadata_cached is False
        assert first.search_fallback is None
        assert [(a.platform, a.status, a.cached) for a in first.attempts] == [
            ("youtube", "verified", False)
        ]

        second = await service.resolve(sample_track.url, "youtube", False)
        assert second.match is not None
        assert second.cached is True
        assert second.metadata_cached is True
        assert second.attempts[0].cached is True
    finally:
        await cache.close()

    assert provider.calls == ['"Rick Astley" "Never Gonna Give You Up" topic']
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_no_match_returns_unverified_search_fallback(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    provider = FakeProvider("youtube", [])
    cache, service, spotify = await _service(
        settings, {"youtube": provider}, sample_track
    )
    try:
        first = await service.resolve(sample_track.url, "youtube", False)
        assert first.match is None
        assert first.search_fallback is not None
        assert first.search_fallback.verified is False
        assert first.search_fallback.platform == "youtube"
        assert first.search_fallback.url.startswith("https://www.youtube.com/results?")
        assert first.cached is False
        assert first.attempts[0].status == "no_match"

        second = await service.resolve(sample_track.url, "youtube", False)
        assert second.match is None
        assert second.cached is True
        assert second.attempts[0].cached is True
    finally:
        await cache.close()
    assert provider.calls == [
        '"Rick Astley" "Never Gonna Give You Up" topic',
        '"Rick Astley" "Never Gonna Give You Up"',
    ]
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_negative_cache_expires(
    make_settings, sample_track: SpotifyTrack
) -> None:
    settings = make_settings(negative_cache_ttl_seconds=10)
    clock = MockClock()
    provider = FakeProvider("youtube", [])
    cache, service, spotify = await _service(
        settings,
        {"youtube": provider},
        sample_track,
        clock=clock,
    )
    try:
        first = await service.resolve(sample_track.url, "youtube", False)
        assert first.match is None
        provider.candidates = [_candidate()]
        clock.advance(11)
        second = await service.resolve(sample_track.url, "youtube", False)
        assert second.match is not None
        assert second.cached is False
        assert second.metadata_cached is True
    finally:
        await cache.close()
    assert provider.calls == [
        '"Rick Astley" "Never Gonna Give You Up" topic',
        '"Rick Astley" "Never Gonna Give You Up"',
        '"Rick Astley" "Never Gonna Give You Up" topic',
    ]
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_match_cache_survives_metadata_expiry(
    make_settings,
    sample_track: SpotifyTrack,
) -> None:
    settings = make_settings(
        metadata_cache_ttl_seconds=10,
        match_cache_ttl_seconds=1_000,
    )
    clock = MockClock()
    provider = FakeProvider("youtube", [_candidate()])
    cache, service, spotify = await _service(
        settings,
        {"youtube": provider},
        sample_track,
        clock=clock,
    )
    try:
        first = await service.resolve(sample_track.url, "youtube", False)
        assert first.match is not None
        clock.advance(20)
        second = await service.resolve(sample_track.url, "youtube", False)
        assert second.match is not None
        assert second.cached is True
        assert second.metadata_cached is True
    finally:
        await cache.close()
    assert provider.calls == ['"Rick Astley" "Never Gonna Give You Up" topic']
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_fallback_cached_match_is_served_when_preferred_is_negative(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    clock = MockClock()
    youtube = FakeProvider("youtube", [])
    soundcloud = FakeProvider("soundcloud", [])
    cache, service, spotify = await _service(
        settings,
        {"youtube": youtube, "soundcloud": soundcloud},
        sample_track,
        clock=clock,
    )
    try:
        await cache.put_resolution(
            sample_track,
            "youtube",
            "not_found",
            candidate=None,
            best_confidence=0.4,
            now=clock.value,
            ttl_seconds=3_600,
        )
        candidate = _candidate(
            platform="soundcloud",
            url="https://soundcloud.com/rick-astley/never-gonna-give-you-up",
            artist="Rick Astley",
            is_topic=False,
        )
        scored = score_candidate(
            sample_track,
            candidate,
            "soundcloud",
            ScoringConfig(),
        )
        assert scored.eligible is True
        await cache.put_resolution(
            sample_track,
            "soundcloud",
            "matched",
            candidate=scored,
            best_confidence=scored.score.total,
            now=clock.value,
            ttl_seconds=3_600,
        )

        result = await service.resolve(sample_track.url, "youtube", True)

        assert result.match is not None
        assert result.match.candidate.platform == "soundcloud"
        assert result.cached is True
        assert result.metadata_cached is True
        assert [(a.platform, a.status, a.cached) for a in result.attempts] == [
            ("youtube", "no_match", True),
            ("soundcloud", "verified", True),
        ]
    finally:
        await cache.close()
    assert spotify.calls == 0
    assert youtube.calls == []
    assert soundcloud.calls == []


@pytest.mark.asyncio
async def test_negative_cache_survives_metadata_expiry(
    make_settings,
    sample_track: SpotifyTrack,
) -> None:
    settings = make_settings(
        metadata_cache_ttl_seconds=10,
        negative_cache_ttl_seconds=100,
    )
    clock = MockClock()
    provider = FakeProvider("youtube", [])
    cache, service, spotify = await _service(
        settings,
        {"youtube": provider},
        sample_track,
        clock=clock,
    )
    try:
        first = await service.resolve(sample_track.url, "youtube", False)
        assert first.match is None
        clock.advance(20)
        second = await service.resolve(sample_track.url, "youtube", False)
        assert second.match is None
        assert second.cached is True
        assert second.metadata_cached is True
    finally:
        await cache.close()
    assert provider.calls == [
        '"Rick Astley" "Never Gonna Give You Up" topic',
        '"Rick Astley" "Never Gonna Give You Up"',
    ]
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_cached_match_is_revalidated_against_current_threshold(
    make_settings,
    sample_track: SpotifyTrack,
) -> None:
    settings = make_settings()
    provider = FakeProvider(
        "youtube",
        [_candidate(duration_ms=223_573, artist="Rick Astley", is_topic=False)],
    )
    cache, service, _spotify = await _service(
        settings,
        {"youtube": provider},
        sample_track,
    )
    try:
        first = await service.resolve(sample_track.url, "youtube", False)
        assert first.match is not None
        assert 0.8 <= first.match.score.total < 0.99
    finally:
        await cache.close()

    stricter = make_settings(match_threshold=0.99)
    stricter_cache, stricter_service, _spotify = await _service(
        stricter,
        {"youtube": provider},
        sample_track,
    )
    provider.candidates = []
    try:
        second = await stricter_service.resolve(sample_track.url, "youtube", False)
        assert second.match is None
        assert second.metadata_cached is True
    finally:
        await stricter_cache.close()
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_spotify_not_found_is_negatively_cached(
    make_settings,
    sample_track: SpotifyTrack,
) -> None:
    settings = make_settings()
    cache = Cache(settings.database_path)
    await cache.initialize()
    spotify = FakeSpotify(error=SpotifyNotFound())
    service = ResolverService(
        settings,
        spotify,  # type: ignore[arg-type]
        cache,
        {"youtube": FakeProvider("youtube", [_candidate()])},
        clock=MockClock(),
    )
    try:
        with pytest.raises(ResolverError) as first:
            await service.resolve(sample_track.url, "youtube", False)
        assert first.value.status_code == 404
        assert first.value.code == "spotify_track_not_found"
        with pytest.raises(ResolverError) as second:
            await service.resolve(sample_track.url, "youtube", False)
        assert second.value.status_code == 404
    finally:
        await cache.close()
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_spotify_timeout_is_not_cached(
    make_settings, sample_track: SpotifyTrack
) -> None:
    settings = make_settings()
    cache = Cache(settings.database_path)
    await cache.initialize()
    spotify = FakeSpotify(error=SpotifyTimeout())
    service = ResolverService(
        settings,
        spotify,  # type: ignore[arg-type]
        cache,
        {"youtube": FakeProvider("youtube", [_candidate()])},
        clock=MockClock(),
    )
    try:
        for _ in range(2):
            with pytest.raises(ResolverError) as error:
                await service.resolve(sample_track.url, "youtube", False)
            assert error.value.status_code == 504
            assert error.value.code == "spotify_timeout"
    finally:
        await cache.close()
    assert spotify.calls == 2


@pytest.mark.asyncio
async def test_provider_failure_is_not_negatively_cached(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    provider = FakeProvider("youtube", error=ProviderTimeout("timeout"))
    cache, service, spotify = await _service(
        settings, {"youtube": provider}, sample_track
    )
    try:
        for _ in range(2):
            with pytest.raises(ResolverError) as error:
                await service.resolve(sample_track.url, "youtube", False)
            assert error.value.status_code == 504
            assert error.value.code == "providers_timeout"
            assert error.value.attempts[0].status == "timeout"
            assert error.value.search_fallback is not None
    finally:
        await cache.close()
    assert len(provider.calls) == 2
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_provider_rate_limit_error(settings, sample_track: SpotifyTrack) -> None:
    provider = FakeProvider("youtube", error=ProviderRateLimited("limited"))
    cache, service, spotify = await _service(
        settings, {"youtube": provider}, sample_track
    )
    try:
        with pytest.raises(ResolverError) as error:
            await service.resolve(sample_track.url, "youtube", False)
        assert error.value.status_code == 503
        assert error.value.code == "providers_rate_limited"
    finally:
        await cache.close()
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_extra_query_failure_keeps_earlier_verified_match(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    provider = FakeProvider(
        "youtube",
        [_candidate(duration_ms=240_573, artist="Rick Astley", is_topic=False)],
        errors=[None, ProviderTimeout("second query failed")],
    )
    cache, service, _spotify = await _service(
        settings,
        {"youtube": provider},
        sample_track,
    )
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is not None
        assert result.attempts[0].status == "verified"
        assert len(provider.calls) == 2
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_provider_auth_error(settings, sample_track: SpotifyTrack) -> None:
    provider = FakeProvider("youtube", error=ProviderAuthError("bad key"))
    cache, service, _spotify = await _service(
        settings,
        {"youtube": provider},
        sample_track,
    )
    try:
        with pytest.raises(ResolverError) as error:
            await service.resolve(sample_track.url, "youtube", False)
        assert error.value.status_code == 503
        assert error.value.code == "providers_auth_error"
        assert error.value.retryable is True
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_cross_platform_fallback(settings, sample_track: SpotifyTrack) -> None:
    youtube = FakeProvider("youtube", [])
    soundcloud_candidate = _candidate(
        "soundcloud",
        url="https://soundcloud.com/rick-astley/never-gonna-give-you-up",
        artist="Rick Astley",
        is_topic=False,
    )
    soundcloud = FakeProvider("soundcloud", [soundcloud_candidate])
    cache, service, spotify = await _service(
        settings,
        {"youtube": youtube, "soundcloud": soundcloud},
        sample_track,
    )
    try:
        first = await service.resolve(sample_track.url, "youtube", True)
        assert first.match is not None
        assert first.match.candidate.platform == "soundcloud"
        assert [(a.platform, a.status) for a in first.attempts] == [
            ("youtube", "no_match"),
            ("soundcloud", "verified"),
        ]
        second = await service.resolve(sample_track.url, "youtube", True)
        assert second.match is not None
        assert second.cached is True
    finally:
        await cache.close()
    assert len(youtube.calls) == 2
    assert len(soundcloud.calls) == 1
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_fallback_disabled_does_not_search_other_platform(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    youtube = FakeProvider("youtube", [])
    soundcloud = FakeProvider(
        "soundcloud",
        [
            _candidate(
                "soundcloud",
                url="https://soundcloud.com/rick-astley/song",
                artist="Rick Astley",
            )
        ],
    )
    cache, service, _spotify = await _service(
        settings,
        {"youtube": youtube, "soundcloud": soundcloud},
        sample_track,
    )
    try:
        result = await service.resolve(sample_track.url, "youtube", False)
        assert result.match is None
        assert result.search_fallback is not None
    finally:
        await cache.close()
    assert len(youtube.calls) == 2
    assert soundcloud.calls == []


@pytest.mark.asyncio
async def test_unconfigured_preferred_platform_uses_fallback(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    youtube = FakeProvider("youtube", configured=False)
    soundcloud = FakeProvider(
        "soundcloud",
        [
            _candidate(
                "soundcloud",
                url="https://soundcloud.com/rick-astley/song",
                artist="Rick Astley",
            )
        ],
    )
    cache, service, spotify = await _service(
        settings,
        {"youtube": youtube, "soundcloud": soundcloud},
        sample_track,
    )
    try:
        result = await service.resolve(sample_track.url, "youtube", True)
        assert result.match is not None
        assert result.match.candidate.platform == "soundcloud"
        assert [(a.platform, a.status) for a in result.attempts] == [
            ("youtube", "not_configured"),
            ("soundcloud", "verified"),
        ]
    finally:
        await cache.close()
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_no_provider_configured(settings, sample_track: SpotifyTrack) -> None:
    cache = Cache(settings.database_path)
    await cache.initialize()
    spotify = FakeSpotify(sample_track)
    service = ResolverService(
        settings,
        spotify,  # type: ignore[arg-type]
        cache,
        {
            "youtube": FakeProvider("youtube", configured=False),
            "soundcloud": FakeProvider("soundcloud", configured=False),
        },
        clock=MockClock(),
    )
    try:
        with pytest.raises(ResolverError) as error:
            await service.resolve(sample_track.url, "youtube", True)
        assert error.value.status_code == 503
        assert error.value.code == "providers_not_configured"
    finally:
        await cache.close()
    assert spotify.calls == 0


@pytest.mark.asyncio
async def test_unconfigured_preferred_without_fallback(
    settings, sample_track: SpotifyTrack
) -> None:
    cache = Cache(settings.database_path)
    await cache.initialize()
    spotify = FakeSpotify(sample_track)
    service = ResolverService(
        settings,
        spotify,  # type: ignore[arg-type]
        cache,
        {
            "youtube": FakeProvider("youtube", configured=False),
            "soundcloud": FakeProvider(
                "soundcloud",
                [
                    _candidate(
                        "soundcloud",
                        url="https://soundcloud.com/artist/song",
                        artist="Rick Astley",
                    )
                ],
                configured=True,
            ),
        },
        clock=MockClock(),
    )
    try:
        with pytest.raises(ResolverError) as error:
            await service.resolve(sample_track.url, "youtube", False)
        assert error.value.status_code == 503
        assert error.value.code == "provider_not_configured"
    finally:
        await cache.close()
    assert spotify.calls == 0


@pytest.mark.asyncio
async def test_both_platforms_no_match_returns_fallback(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    youtube = FakeProvider("youtube", [])
    soundcloud = FakeProvider("soundcloud", [])
    cache, service, _spotify = await _service(
        settings,
        {"youtube": youtube, "soundcloud": soundcloud},
        sample_track,
    )
    try:
        result = await service.resolve(sample_track.url, "youtube", True)
        assert result.match is None
        assert result.search_fallback is not None
        assert [(a.platform, a.status) for a in result.attempts] == [
            ("youtube", "no_match"),
            ("soundcloud", "no_match"),
        ]
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_concurrent_requests_deduplicate_provider_and_metadata_calls(
    settings,
    sample_track: SpotifyTrack,
) -> None:
    provider = FakeProvider("youtube", [_candidate()], delay=0.05)
    cache, service, spotify = await _service(
        settings, {"youtube": provider}, sample_track
    )
    try:
        results = await asyncio.gather(
            *(service.resolve(sample_track.url, "youtube", False) for _ in range(8))
        )
    finally:
        await cache.close()
    assert all(result.match is not None for result in results)
    assert len(provider.calls) == 1
    assert spotify.calls == 1


@pytest.mark.asyncio
async def test_invalid_url_and_platform(settings, sample_track: SpotifyTrack) -> None:
    cache, service, spotify = await _service(
        settings,
        {"youtube": FakeProvider("youtube", [_candidate()])},
        sample_track,
    )
    try:
        with pytest.raises(ResolverError) as invalid_url:
            await service.resolve(
                "https://open.spotify.com/album/abc", "youtube", False
            )
        assert invalid_url.value.status_code == 422
        with pytest.raises(ResolverError) as invalid_platform:
            await service.resolve(sample_track.url, "tidal", False)
        assert invalid_platform.value.status_code == 400
    finally:
        await cache.close()
    assert spotify.calls == 0
