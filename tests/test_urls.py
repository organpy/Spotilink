from __future__ import annotations

import pytest

from spotify_resolver.urls import InputKind, UnsupportedInput, classify_url

SPOTIFY_ID = "37i9dQZF1DWXRqgorJj26U"
PL = "PL4fGSI1pDJn6O1LS0XSdF3RyO0Rq_LDeI"


@pytest.mark.parametrize(
    ("url", "kind", "identifier"),
    [
        (f"https://open.spotify.com/track/{SPOTIFY_ID}", InputKind.SPOTIFY_TRACK, SPOTIFY_ID),
        (f"https://open.spotify.com/track/{SPOTIFY_ID}?si=abc", InputKind.SPOTIFY_TRACK, SPOTIFY_ID),
        (f"https://open.spotify.com/intl-fr/track/{SPOTIFY_ID}", InputKind.SPOTIFY_TRACK, SPOTIFY_ID),
        (f"https://open.spotify.com/playlist/{SPOTIFY_ID}", InputKind.SPOTIFY_PLAYLIST, SPOTIFY_ID),
        (f"  http://OPEN.SPOTIFY.COM./playlist/{SPOTIFY_ID}/  ", InputKind.SPOTIFY_PLAYLIST, SPOTIFY_ID),
        (f"https://www.youtube.com/playlist?list={PL}", InputKind.YOUTUBE_PLAYLIST, PL),
        (f"https://youtube.com/playlist?list={PL}&si=x", InputKind.YOUTUBE_PLAYLIST, PL),
        (f"https://m.youtube.com/playlist?list={PL}", InputKind.YOUTUBE_PLAYLIST, PL),
        (f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PL}&index=3", InputKind.YOUTUBE_PLAYLIST, PL),
        (f"https://youtu.be/dQw4w9WgXcQ?list={PL}", InputKind.YOUTUBE_PLAYLIST, PL),
        (
            "https://music.youtube.com/playlist?list=OLAK5uy_miEsiLe0SQGnjve2_T9C9QfvovWhoTsSY",
            InputKind.YOUTUBE_PLAYLIST,
            "OLAK5uy_miEsiLe0SQGnjve2_T9C9QfvovWhoTsSY",
        ),
    ],
)
def test_supported(url: str, kind: InputKind, identifier: str) -> None:
    result = classify_url(url)
    assert result.kind is kind and result.identifier == identifier


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube_video_not_supported"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube_video_not_supported"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube_video_not_supported"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "youtube_video_not_supported"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ", "unsupported_input"),
        ("https://www.youtube.com/playlist?list=WL", "unsupported_input"),
        ("https://www.youtube.com/playlist?list=LL", "unsupported_input"),
        (f"https://open.spotify.com/album/{SPOTIFY_ID}", "unsupported_input"),
        (f"https://open.spotify.com/artist/{SPOTIFY_ID}", "unsupported_input"),
        ("https://open.spotify.com/playlist/short", "invalid_url"),
        # Lookalike / substring hosts must not pass the allowlist.
        (f"https://open.spotify.com.evil.example/playlist/{SPOTIFY_ID}", "unsupported_input"),
        (f"https://evil.example/open.spotify.com/playlist/{SPOTIFY_ID}", "unsupported_input"),
        (f"https://evil.example/?u=https://www.youtube.com/playlist?list={PL}", "unsupported_input"),
        (f"https://notyoutube.com/playlist?list={PL}", "unsupported_input"),
        (f"https://user:pw@open.spotify.com/playlist/{SPOTIFY_ID}", "invalid_url"),
        (f"https://open.spotify.com:8443/playlist/{SPOTIFY_ID}", "invalid_url"),
        (f"ftp://open.spotify.com/playlist/{SPOTIFY_ID}", "invalid_url"),
        (f"spotify:playlist:{SPOTIFY_ID}", "invalid_url"),
        ("", "invalid_url"),
        ("https://" + "a" * 3000, "invalid_url"),
        ("http://[::1", "invalid_url"),
    ],
)
def test_rejected(url: str, code: str) -> None:
    with pytest.raises(UnsupportedInput) as info:
        classify_url(url)
    assert info.value.code == code


def test_canonical_urls_are_rebuilt_from_ids() -> None:
    result = classify_url(f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PL}&evil=1")
    assert result.canonical_url == f"https://www.youtube.com/playlist?list={PL}"
    assert result.source == "youtube"
