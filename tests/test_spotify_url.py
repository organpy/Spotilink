from __future__ import annotations

import pytest

from spotify_resolver.errors import InvalidSpotifyUrl
from spotify_resolver.spotify import parse_spotify_track_url

TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/track/{TRACK_ID}",
        f"http://open.spotify.com/track/{TRACK_ID}",
        f"https://OPEN.SPOTIFY.COM/track/{TRACK_ID}",
        f"https://open.spotify.com/track/{TRACK_ID}/",
        f"https://open.spotify.com/track/{TRACK_ID}?si=abc123",
        f"https://open.spotify.com/intl-de/track/{TRACK_ID}",
        f"https://open.spotify.com/intl-pt-BR/track/{TRACK_ID}?si=x",
        f"https://open.spotify.com:443/track/{TRACK_ID}",
        f"  https://open.spotify.com/track/{TRACK_ID}  ",
    ],
)
def test_parse_valid_track_urls(url: str) -> None:
    assert parse_spotify_track_url(url) == TRACK_ID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        "ftp://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://example.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com.evil.example/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://user@open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com:8443/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/album/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/artist/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/playlist/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/embed/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/track/",
        "https://open.spotify.com/track/short",
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQ",
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQCC",
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC/extra",
        "https://open.spotify.com/foo/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC%2Fextra",
        "https://open.spotify.com/track/" + "!" * 22,
    ],
)
def test_parse_invalid_track_urls(url: str) -> None:
    with pytest.raises(InvalidSpotifyUrl):
        parse_spotify_track_url(url)


def test_parse_rejects_non_string() -> None:
    with pytest.raises(InvalidSpotifyUrl):
        parse_spotify_track_url(None)  # type: ignore[arg-type]
