from __future__ import annotations

from dataclasses import replace

import pytest

from spotify_resolver.matching import (
    ScoringConfig,
    artist_similarity,
    best_accepted,
    build_queries,
    clean_title,
    rank_candidates,
    score_candidate,
    search_fallback_url,
    title_similarity,
    version_tags,
)
from spotify_resolver.models import Candidate, SpotifyTrack


def candidate(
    *,
    title: str = "Never Gonna Give You Up",
    artist: str = "Rick Astley",
    duration_ms: int | None = 213_573,
    url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    platform: str = "youtube",
    is_topic: bool = False,
    is_vevo: bool = False,
    official_hint: bool = False,
) -> Candidate:
    return Candidate(
        platform=platform,
        url=url,
        title=title,
        artist=artist,
        uploader=artist,
        duration_ms=duration_ms,
        is_topic=is_topic,
        is_vevo=is_vevo,
        official_hint=official_hint,
    )


def test_exact_topic_candidate_is_accepted(sample_track: SpotifyTrack) -> None:
    config = ScoringConfig()
    scored = score_candidate(
        sample_track,
        candidate(artist="Rick Astley - Topic", is_topic=True),
        "youtube",
        config,
    )
    assert scored.eligible is True
    assert scored.score.total == 1.0
    assert scored.score.title == 1.0
    assert scored.score.artist == 1.0
    assert scored.score.official_bonus > 0
    assert best_accepted([scored], config) is scored


def test_wrong_artist_is_not_eligible(sample_track: SpotifyTrack) -> None:
    scored = score_candidate(
        sample_track,
        candidate(artist="Some Other Band"),
        "youtube",
        ScoringConfig(),
    )
    assert scored.eligible is False
    assert scored.rejection_reason == "artist_mismatch"
    assert scored.score.artist < 0.5


def test_duration_outside_cap_is_rejected(sample_track: SpotifyTrack) -> None:
    scored = score_candidate(
        sample_track,
        candidate(duration_ms=250_000),
        "youtube",
        ScoringConfig(),
    )
    assert scored.eligible is False
    assert scored.rejection_reason == "duration_mismatch"
    assert scored.score.duration == 0.0
    assert scored.score.duration_diff_ms == 36_427


@pytest.mark.parametrize(
    "title,artist",
    [
        ("Never Gonna Give You Up (Live)", "Rick Astley"),
        ("Never Gonna Give You Up (Remix)", "Rick Astley"),
        ("Never Gonna Give You Up (Instrumental)", "Rick Astley"),
        ("Never Gonna Give You Up (Cover)", "Rick Astley"),
        ("Never Gonna Give You Up (Karaoke)", "Rick Astley"),
    ],
)
def test_version_variants_are_penalized(
    sample_track: SpotifyTrack,
    title: str,
    artist: str,
) -> None:
    config = ScoringConfig()
    scored = score_candidate(
        sample_track,
        candidate(title=title, artist=artist),
        "youtube",
        config,
    )
    assert scored.score.version_penalty >= 0.25
    assert scored.score.total < config.threshold
    assert best_accepted([scored], config) is None


def test_expected_live_version_is_accepted(sample_track: SpotifyTrack) -> None:
    live_track = replace(sample_track, title="Never Gonna Give You Up (Live)")
    scored = score_candidate(
        live_track,
        candidate(title="Never Gonna Give You Up (Live)"),
        "youtube",
        ScoringConfig(),
    )
    assert "live" in scored.score.version_tags
    assert scored.score.version_penalty == 0.0
    assert scored.eligible is True
    assert best_accepted([scored], ScoringConfig()) is scored


def test_remastered_version_is_mildly_penalized(sample_track: SpotifyTrack) -> None:
    config = ScoringConfig()
    scored = score_candidate(
        sample_track,
        candidate(title="Never Gonna Give You Up (Remastered 2011)"),
        "youtube",
        config,
    )
    assert scored.score.version_penalty == 0.02
    assert scored.score.total >= config.threshold


def test_missing_duration_is_capped_and_rejected(sample_track: SpotifyTrack) -> None:
    config = ScoringConfig()
    scored = score_candidate(
        sample_track,
        candidate(duration_ms=None),
        "youtube",
        config,
    )
    assert scored.score.capped is True
    assert scored.score.duration is None
    assert scored.score.total == config.no_duration_score_cap
    assert scored.eligible is False
    assert scored.rejection_reason == "duration_missing"
    assert best_accepted([scored], config) is None

    permissive = ScoringConfig(require_duration=False)
    permissive_scored = score_candidate(
        sample_track,
        candidate(duration_ms=None),
        "youtube",
        permissive,
    )
    assert permissive_scored.eligible is True
    assert best_accepted([permissive_scored], permissive) is permissive_scored


def test_threshold_is_configurable(sample_track: SpotifyTrack) -> None:
    scored = score_candidate(
        sample_track,
        candidate(title="Never Gonna Give You Up (Live)"),
        "youtube",
        ScoringConfig(),
    )
    assert best_accepted([scored], ScoringConfig(threshold=0.8)) is None
    assert best_accepted([scored], ScoringConfig(threshold=0.6)) is scored


def test_soundcloud_duration_cap_is_stricter(sample_track: SpotifyTrack) -> None:
    config = ScoringConfig()
    too_long = score_candidate(
        sample_track,
        candidate(
            platform="soundcloud",
            url="https://soundcloud.com/rick-astley/never-gonna-give-you-up",
            duration_ms=229_573,
        ),
        "soundcloud",
        config,
    )
    assert too_long.eligible is False
    assert too_long.score.duration_diff_ms == 16_000

    acceptable = score_candidate(
        sample_track,
        candidate(
            platform="soundcloud",
            url="https://soundcloud.com/rick-astley/never-gonna-give-you-up",
            duration_ms=227_573,
        ),
        "soundcloud",
        config,
    )
    assert acceptable.eligible is True
    assert best_accepted([acceptable], config) is acceptable


def test_title_and_artist_normalization() -> None:
    assert title_similarity("Song (feat. Guest)", "Song") == 1.0
    assert title_similarity("Song (Official Video)", "Song") == 1.0
    assert title_similarity("Song (Live)", "Song") < 1.0
    assert artist_similarity("Rick Astley - Topic", "Rick Astley") == 1.0
    assert artist_similarity("Rick Astley", "RickAstleyVEVO") == 1.0
    assert artist_similarity("Simon & Garfunkel", "Simon and Garfunkel") == 1.0


def test_delivery_noise_removal_is_context_aware() -> None:
    assert clean_title("Clean") == "Clean"
    assert clean_title("Audio") == "Audio"
    assert clean_title("Lyrics") == "Lyrics"
    assert clean_title("Song (Official Video)") == "Song"
    assert clean_title("Song [Official Audio]") == "Song"
    assert clean_title("Song - Official Video") == "Song"
    assert clean_title("Song | Lyrics") == "Song"
    assert clean_title("Song (Live)") == "Song (Live)"
    assert title_similarity("Clean", "Clean (Official Video)") == 1.0


def test_version_tags() -> None:
    assert "live" in version_tags("Song (Live at Wembley)")
    assert "remix" in version_tags("Song (Somebody Remix)")
    assert "remaster" in version_tags("Song (Remastered 2011)")
    assert "8d" in version_tags("Song (8D Audio)")
    assert version_tags("Song") == ()


def test_ranking_prefers_valid_official_candidate(sample_track: SpotifyTrack) -> None:
    config = ScoringConfig()
    user = candidate(
        url="https://www.youtube.com/watch?v=user",
        duration_ms=220_000,
    )
    topic = candidate(
        url="https://www.youtube.com/watch?v=topic",
        artist="Rick Astley - Topic",
        duration_ms=220_000,
        is_topic=True,
    )
    ranked = rank_candidates(sample_track, [user, topic], "youtube", config)
    assert ranked[0].candidate.url == topic.url
    assert best_accepted(ranked, config) is ranked[0]


def test_ranking_deduplicates_urls_and_versions(sample_track: SpotifyTrack) -> None:
    config = ScoringConfig()
    first = candidate(url="https://www.youtube.com/watch?v=a")
    duplicate = candidate(url="https://www.youtube.com/watch?v=a", artist="Someone")
    same_version = candidate(url="https://www.youtube.com/watch?v=b")
    ranked = rank_candidates(
        sample_track, [first, duplicate, same_version], "youtube", config
    )
    assert len(ranked) == 1


def test_build_queries(sample_track: SpotifyTrack) -> None:
    youtube = build_queries(sample_track, "youtube", 2)
    assert youtube == [
        '"Rick Astley" "Never Gonna Give You Up" topic',
        '"Rick Astley" "Never Gonna Give You Up"',
    ]
    soundcloud = build_queries(sample_track, "soundcloud", 2)
    assert soundcloud == [
        "Rick Astley Never Gonna Give You Up",
        "Never Gonna Give You Up",
    ]


def test_search_fallback_urls(sample_track: SpotifyTrack) -> None:
    youtube = search_fallback_url("youtube", sample_track)
    soundcloud = search_fallback_url("soundcloud", sample_track)
    assert youtube.startswith("https://www.youtube.com/results?search_query=")
    assert "Rick+Astley" in youtube
    assert "Never+Gonna" in youtube
    assert soundcloud.startswith("https://soundcloud.com/search?q=")


import pytest as _pytest  # noqa: E402

from spotify_resolver.matching import clean_title as _clean_title  # noqa: E402


@_pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Imagine - Remastered 2010", "Imagine"),
        ("Hey Jude - Remastered 2015", "Hey Jude"),
        ("Superstition - Single Version", "Superstition"),
        ("Heroes - Single Version;1990 Remastered Version", "Heroes"),
        ("Song (Remastered 2009)", "Song"),
    ],
)
def test_edition_annotations_are_stripped(raw: str, cleaned: str) -> None:
    assert _clean_title(raw) == cleaned


@_pytest.mark.parametrize(
    "raw",
    ["Despacito - Remix", "Song - Acoustic Version", "Hotel California - Live On MTV, 1994", "Song - Original Mix"],
)
def test_meaningful_version_markers_are_kept(raw: str) -> None:
    assert _clean_title(raw) == raw
