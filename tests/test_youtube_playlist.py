from __future__ import annotations

import pytest

from spotify_resolver.playlists.models import (
    PlaylistEmpty,
    PlaylistNotFound,
    PlaylistUpstreamBlocked,
    PlaylistUpstreamUnavailable,
)
from spotify_resolver.playlists.youtube_playlist import (
    YouTubePlaylistExtractor,
    classify_ytdlp_error,
    ydl_options,
)

PL = "PL4fGSI1pDJn6O1LS0XSdF3RyO0Rq_LDeI"


def _entry(video_id: str, title: str = "Song", **extra):
    return {"_type": "url", "id": video_id, "title": title, "channel": "Chan", "duration": 201, **extra}


def _runner(info=None, error: Exception | None = None, seen: list | None = None):
    def run(url, options):
        if seen is not None:
            seen.append((url, options))
        if error is not None:
            raise error
        return info

    return run


async def test_flat_extraction_options_never_download(settings) -> None:
    seen: list = []
    info = {"_type": "playlist", "title": "T", "playlist_count": 1, "entries": [_entry("dQw4w9WgXcQ")]}
    extractor = YouTubePlaylistExtractor(settings, runner=_runner(info, seen=seen))
    await extractor.extract(PL, 100)
    url, options = seen[0]
    assert url == f"https://www.youtube.com/playlist?list={PL}"
    assert options["extract_flat"] == "in_playlist"
    assert options["skip_download"] is True
    assert options["playlistend"] == 100
    assert "format" not in options and "postprocessors" not in options
    assert ydl_options(5)["cachedir"] is False


async def test_order_duplicates_and_unavailable_entries(settings) -> None:
    entries = [
        _entry("aaaaaaaaaaa", "First"),
        _entry("bbbbbbbbbbb", "[Private video]", duration=None, channel=None),
        _entry("aaaaaaaaaaa", "First"),
        _entry("ccccccccccc", "[Deleted video]"),
        _entry("ddddddddddd", "Members", availability="subscriber_only"),
        {"id": "not-valid", "title": "?"},
    ]
    info = {"_type": "playlist", "title": "Mix", "channel": "Me", "playlist_count": 6, "entries": entries}
    result = await YouTubePlaylistExtractor(settings, runner=_runner(info)).extract(PL, 100)
    assert result.extraction_complete
    assert [e.position for e in result.entries] == [1, 2, 3, 4, 5, 6]
    assert [e.source_id for e in result.entries[:3]] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "aaaaaaaaaaa"]
    assert [e.unavailable_reason for e in result.entries] == [
        None, "private", None, "deleted", "members_only", "invalid_id",
    ]
    first = result.entries[0]
    assert first.url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert first.duration_ms == 201_000 and first.artists == ("Chan",)
    assert result.entries[1].title is None


async def test_hidden_videos_make_extraction_incomplete(settings) -> None:
    info = {"_type": "playlist", "playlist_count": 3, "entries": [_entry("aaaaaaaaaaa")]}
    result = await YouTubePlaylistExtractor(settings, runner=_runner(info)).extract(PL, 100)
    assert not result.extraction_complete and result.incomplete_reason == "count_mismatch"


async def test_truncation(settings) -> None:
    info = {"_type": "playlist", "playlist_count": 500, "entries": [_entry("aaaaaaaaaaa")] * 10}
    result = await YouTubePlaylistExtractor(settings, runner=_runner(info)).extract(PL, 10)
    assert result.truncated and not result.extraction_complete


async def test_empty_playlist(settings) -> None:
    info = {"_type": "playlist", "playlist_count": 0, "entries": []}
    with pytest.raises(PlaylistEmpty):
        await YouTubePlaylistExtractor(settings, runner=_runner(info)).extract(PL, 10)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: [youtube:tab] PLx: YouTube said: The playlist does not exist.", PlaylistNotFound),
        ("ERROR: This playlist is private", PlaylistNotFound),
        ("ERROR: HTTP Error 429: Too Many Requests", PlaylistUpstreamBlocked),
        ("ERROR: Sign in to confirm you're not a bot", PlaylistUpstreamBlocked),
        ("ERROR: Unable to download webpage: timed out", PlaylistUpstreamUnavailable),
        ("ERROR: something odd", PlaylistUpstreamUnavailable),
    ],
)
def test_error_classification(message: str, expected: type) -> None:
    assert isinstance(classify_ytdlp_error(Exception(message)), expected)


async def test_runner_errors_are_mapped(settings) -> None:
    extractor = YouTubePlaylistExtractor(
        settings, runner=_runner(error=Exception("YouTube said: The playlist does not exist."))
    )
    with pytest.raises(PlaylistNotFound):
        await extractor.extract(PL, 10)
