from __future__ import annotations

import asyncio

import pytest
from conftest import FakeYTMusic, ytm_candidate

from spotify_resolver.config import Settings
from spotify_resolver.errors import (
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUpstreamError,
)
from spotify_resolver.models import SpotifyTrack
from spotify_resolver.providers.ytmusic import (
    YTMusicClient,
    YTMusicMatcher,
    build_query,
    classify_exception,
    parse_results,
)


def _track(title: str, artists: tuple[str, ...], duration_ms: int = 213_000) -> SpotifyTrack:
    return SpotifyTrack("4uLU6hMCjMI75M1A2tKUQC", title, artists, None, duration_ms, "ms")


def _matcher(fake: FakeYTMusic) -> YTMusicMatcher:
    return YTMusicMatcher.from_settings(fake, Settings())  # type: ignore[arg-type]


def test_parse_results_maps_songs_and_videos() -> None:
    raw = [
        {
            "resultType": "song",
            "videoId": "swJOIjjW69U",
            "title": "Dreams (2004 Remaster)",
            "artists": [{"name": "Fleetwood Mac", "id": "x"}],
            "duration_seconds": 258,
            "videoType": "MUSIC_VIDEO_TYPE_ATV",
        },
        {
            "resultType": "video",
            "videoId": "Y3ywicffOj4",
            "title": "Dreams",
            "artists": [{"name": "Fleetwood Mac"}, {"name": "Stevie Nicks"}],
            "duration_seconds": 264,
            "videoType": "MUSIC_VIDEO_TYPE_OMV",
        },
        {"resultType": "album", "browseId": "x", "title": "Rumours"},
        {"resultType": "song", "videoId": "bad id!", "title": "x"},
        "garbage",
    ]
    song, video = parse_results(raw, "songs")
    assert song.url == "https://www.youtube.com/watch?v=swJOIjjW69U"
    assert song.is_topic and song.duration_ms == 258_000 and song.source == "ytmusicapi"
    assert video.official_hint and not video.is_topic
    assert video.artist == "Fleetwood Mac, Stevie Nicks"


def test_parse_results_rejects_non_list() -> None:
    with pytest.raises(ProviderUpstreamError):
        parse_results({"error": "x"}, "songs")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (type("ReadTimeout", (Exception,), {})("t"), ProviderTimeout),
        (Exception("Server returned HTTP 429: Too Many Requests."), ProviderRateLimited),
        (type("ConnectionError", (Exception,), {})("c"), ProviderUpstreamError),
        (KeyError("contents"), ProviderUpstreamError),
    ],
)
def test_classify_exception(exc: Exception, expected: type) -> None:
    assert isinstance(classify_exception(exc), expected)


def test_build_query_strips_featuring_but_keeps_versions() -> None:
    assert build_query(_track("Levitating (feat. DaBaby)", ("Dua Lipa", "DaBaby"))) == (
        "Dua Lipa Levitating"
    )
    assert build_query(_track("Despacito - Remix", ("Luis Fonsi",))) == "Luis Fonsi Despacito - Remix"


async def test_exact_song_match_skips_video_search() -> None:
    fake = FakeYTMusic(songs=[ytm_candidate()])
    outcome = await _matcher(fake).match(_track("Never Gonna Give You Up", ("Rick Astley",)))
    assert outcome.status == "matched"
    assert outcome.match is not None and outcome.match.score.total >= 0.95
    assert [kind for _, kind in fake.calls] == ["songs"]


async def test_does_not_pick_first_result_blindly() -> None:
    fake = FakeYTMusic(
        songs=[
            ytm_candidate("Never Gonna Give You Up (Karaoke Version)", "Sing King", video_id="aaaaaaaaaaa"),
            ytm_candidate("Never Gonna Give You Up", "Rick Astley", video_id="bbbbbbbbbbb"),
        ]
    )
    outcome = await _matcher(fake).match(_track("Never Gonna Give You Up", ("Rick Astley",)))
    assert outcome.match is not None
    assert outcome.match.candidate.url.endswith("bbbbbbbbbbb")


async def test_multiple_artists_and_featuring() -> None:
    fake = FakeYTMusic(songs=[ytm_candidate("Die With A Smile", "Lady Gaga, Bruno Mars", duration_ms=251_000)])
    outcome = await _matcher(fake).match(_track("Die With A Smile", ("Lady Gaga", "Bruno Mars"), 251_667))
    assert outcome.status == "matched"


@pytest.mark.parametrize(
    "bad_title",
    [
        "Never Gonna Give You Up (Remix)",
        "Never Gonna Give You Up (Live at Wembley)",
        "Never Gonna Give You Up (Cover)",
        "Never Gonna Give You Up (Slowed + Reverb)",
        "Never Gonna Give You Up (Sped Up)",
    ],
)
async def test_unwanted_versions_are_not_matched(bad_title: str) -> None:
    fake = FakeYTMusic(songs=[ytm_candidate(bad_title)], videos=[ytm_candidate(bad_title, kind="video")])
    outcome = await _matcher(fake).match(_track("Never Gonna Give You Up", ("Rick Astley",)))
    assert outcome.status != "matched"


async def test_requested_remix_matches_remix() -> None:
    fake = FakeYTMusic(
        songs=[
            ytm_candidate("Despacito", "Luis Fonsi, Daddy Yankee", video_id="aaaaaaaaaaa", duration_ms=228_000),
            ytm_candidate(
                "Despacito (Remix) (feat. Justin Bieber)",
                "Luis Fonsi, Daddy Yankee",
                video_id="bbbbbbbbbbb",
                duration_ms=228_000,
            ),
        ]
    )
    outcome = await _matcher(fake).match(_track("Despacito - Remix", ("Luis Fonsi", "Daddy Yankee"), 228_200))
    assert outcome.match is not None and outcome.match.candidate.url.endswith("bbbbbbbbbbb")


async def test_punctuation_and_unicode_normalization() -> None:
    fake = FakeYTMusic(songs=[ytm_candidate("Cafe Del Mar", "Energy 52", duration_ms=200_000)])
    outcome = await _matcher(fake).match(_track("Café del Mar!", ("Energy 52",), 200_500))
    assert outcome.status == "matched"
    decomposed = "Café del Mar"  # combining accent
    outcome = await _matcher(fake).match(_track(decomposed, ("Energy 52",), 200_500))
    assert outcome.status == "matched"


async def test_duration_mismatch_is_rejected() -> None:
    fake = FakeYTMusic(songs=[ytm_candidate(duration_ms=400_000)], videos=[])
    outcome = await _matcher(fake).match(_track("Never Gonna Give You Up", ("Rick Astley",)))
    assert outcome.status in {"unmatched", "uncertain"}
    assert outcome.match is None


async def test_falls_back_to_video_results() -> None:
    fake = FakeYTMusic(
        songs=[],
        videos=[ytm_candidate(kind="video", video_type="MUSIC_VIDEO_TYPE_OMV", artist="Rick Astley")],
    )
    outcome = await _matcher(fake).match(_track("Never Gonna Give You Up", ("Rick Astley",)))
    assert outcome.status == "matched"
    assert [kind for _, kind in fake.calls] == ["songs", "videos"]


async def test_long_compilation_title_is_not_a_match() -> None:
    fake = FakeYTMusic(
        videos=[
            ytm_candidate(
                "French Chanson Music Midnight Paris Café Vibes & Timeless 1950s French Love Songs",
                "Zaz Music",
                kind="video",
                duration_ms=180_000,
            )
        ]
    )
    outcome = await _matcher(fake).match(_track("Café", ("Zaz",), 180_000))
    assert outcome.match is None


async def test_unavailable_song_is_unmatched() -> None:
    outcome = await _matcher(FakeYTMusic()).match(_track("Never Gonna Give You Up", ("Rick Astley",)))
    assert outcome.status == "unmatched" and outcome.best is None


async def test_provider_failure_is_reported_not_raised() -> None:
    fake = FakeYTMusic(error=ProviderRateLimited("throttled", upstream=True))
    outcome = await _matcher(fake).match(_track("Never Gonna Give You Up", ("Rick Astley",)))
    assert outcome.status == "failed"
    assert isinstance(outcome.error, ProviderRateLimited)


async def test_client_bounds_concurrency_and_reuses_thread_clients() -> None:
    active = 0
    peak = 0
    created = 0

    class Slow:
        def search(self, query, filter, limit):  # noqa: A002 - ytmusicapi signature
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            import time

            time.sleep(0.05)
            active -= 1
            return []

    def factory():
        nonlocal created
        created += 1
        return Slow()

    client = YTMusicClient(Settings(yt_music_concurrency=2), factory=factory)
    await asyncio.gather(*(client.search("q", "songs", 5) for _ in range(6)))
    assert peak <= 2
    assert 1 <= created <= 6


async def test_client_maps_sync_errors() -> None:
    class Broken:
        def search(self, query, filter, limit):  # noqa: A002
            raise Exception("Server returned HTTP 429: Too Many Requests.")

    client = YTMusicClient(Settings(), factory=Broken)
    with pytest.raises(ProviderRateLimited):
        await client.search("q", "songs", 5)
