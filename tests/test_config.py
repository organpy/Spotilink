from __future__ import annotations

import pytest

from spotify_resolver.config import Settings


def test_settings_from_env() -> None:
    settings = Settings.from_env(
        {
            "DATABASE_PATH": "/tmp/resolver.db",
            "YOUTUBE_API_KEY": "yt-key",
            "SOUNDCLOUD_CLIENT_ID": "sc-id",
            "SOUNDCLOUD_CLIENT_SECRET": "sc-secret",
            "MATCH_THRESHOLD": "0.9",
            "API_RATE_LIMIT_PER_MINUTE": "12",
            "CORS_ORIGINS": "https://a.example, https://b.example",
            "TRUST_PROXY_HEADERS": "true",
            "YOUTUBE_RATE_LIMIT_PER_MINUTE": "0",
            "REQUIRE_DURATION": "false",
            "STALE_MATCH_GRACE_SECONDS": "30",
        }
    )
    assert str(settings.database_path) == "/tmp/resolver.db"
    assert settings.youtube_api_key == "yt-key"
    assert settings.youtube_max_queries == 1
    assert settings.soundcloud_client_id == "sc-id"
    assert settings.match_threshold == 0.9
    assert settings.stale_match_grace_seconds == 30
    assert settings.api_rate_limit_per_minute == 12
    assert settings.cors_origins == ("https://a.example", "https://b.example")
    assert settings.trust_proxy_headers is True
    assert settings.youtube_rate_limit_per_minute == 0
    assert settings.require_duration is False


def test_empty_optional_values_become_none() -> None:
    settings = Settings.from_env({"YOUTUBE_API_KEY": "  ", "SOUNDCLOUD_CLIENT_ID": ""})
    assert settings.youtube_api_key is None
    assert settings.soundcloud_client_id is None


def test_invalid_settings_are_rejected() -> None:
    with pytest.raises(ValueError):
        Settings.from_env({"API_RATE_LIMIT_PER_MINUTE": "not-an-int"})
    with pytest.raises(ValueError):
        Settings.from_env({"MATCH_THRESHOLD": "2"})
    with pytest.raises(ValueError):
        Settings.from_env({"TRUST_PROXY_HEADERS": "maybe"})
    with pytest.raises(ValueError):
        Settings.from_env({"WEIGHT_TITLE": "0", "WEIGHT_ARTIST": "0"})
    with pytest.raises(ValueError):
        Settings.from_env({"YOUTUBE_MAX_DURATION_DIFF_MS": "-1"})
    with pytest.raises(ValueError):
        Settings.from_env({"SPOTIFY_MAX_REDIRECTS": "-1"})
    with pytest.raises(ValueError):
        Settings.from_env({"STALE_MATCH_GRACE_SECONDS": "-1"})


def test_v2_defaults_and_overrides() -> None:
    defaults = Settings.from_env({})
    assert defaults.yt_music_enabled is True
    assert defaults.yt_music_concurrency == 3
    assert defaults.spotify_playlist_extractor == "auto"
    assert defaults.playlist_max_tracks == 1000
    assert defaults.primary_resolver_cooldown == 300
    custom = Settings.from_env(
        {
            "YT_MUSIC_ENABLED": "false",
            "SPOTIFY_PLAYLIST_EXTRACTOR": "WebPlayer",
            "PLAYLIST_MAX_TRACKS": "50",
            "PRIMARY_RESOLVER_COOLDOWN": "0",
        }
    )
    assert custom.yt_music_enabled is False
    assert custom.spotify_playlist_extractor == "webplayer"
    assert custom.playlist_max_tracks == 50 and custom.primary_resolver_cooldown == 0


@pytest.mark.parametrize(
    "env",
    [
        {"SPOTIFY_PLAYLIST_EXTRACTOR": "magic"},
        {"PLAYLIST_MAX_TRACKS": "0"},
        {"YT_MUSIC_MATCH_THRESHOLD": "1.5"},
        {"YT_MUSIC_UNCERTAIN_THRESHOLD": "0.9", "YT_MUSIC_MATCH_THRESHOLD": "0.5"},
        {"SPOTIFY_PLAYLIST_PAGE_SIZE": "1000"},
        {"PRIMARY_RESOLVER_COOLDOWN": "-1"},
    ],
)
def test_v2_invalid_values_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Settings.from_env(env)
