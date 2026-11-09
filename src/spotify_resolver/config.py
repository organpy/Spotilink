from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _optional(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _number(env: Mapping[str, str], name: str, default: float) -> float:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (true/false)")


def _csv(
    env: Mapping[str, str], name: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    # Storage
    database_path: Path = Path("data/resolver.db")

    # HTTP
    http_user_agent: str = "spotilink/0.1"
    http_connect_timeout_seconds: float = 5.0
    http_max_retries: int = 1
    http_retry_backoff_seconds: float = 0.5
    http_retry_after_cap_seconds: float = 2.0

    # Spotify page extraction
    spotify_timeout_seconds: float = 10.0
    spotify_max_bytes: int = 2_097_152
    spotify_max_redirects: int = 3

    # YouTube
    youtube_api_key: str | None = field(default=None, repr=False)
    youtube_max_results: int = 10
    youtube_max_queries: int = 1
    youtube_rate_limit_per_minute: int = 10
    youtube_rate_limit_burst: int = 2

    # SoundCloud
    soundcloud_client_id: str | None = field(default=None, repr=False)
    soundcloud_client_secret: str | None = field(default=None, repr=False)
    soundcloud_max_results: int = 20
    soundcloud_max_queries: int = 2
    soundcloud_rate_limit_per_minute: int = 30
    soundcloud_rate_limit_burst: int = 2

    # Providers
    provider_timeout_seconds: float = 8.0
    provider_max_bytes: int = 1_048_576
    provider_rate_limit_wait_seconds: float = 2.0

    # Matching
    match_threshold: float = 0.80
    weight_title: float = 0.50
    weight_artist: float = 0.35
    weight_duration: float = 0.15
    official_bonus: float = 0.06
    min_title_score: float = 0.65
    min_artist_score: float = 0.50
    no_duration_score_cap: float = 0.85
    require_duration: bool = True
    search_extra_query_margin: float = 0.10
    youtube_max_duration_diff_ms: int = 30_000
    soundcloud_max_duration_diff_ms: int = 15_000

    # Cache
    match_cache_ttl_seconds: int = 604_800
    metadata_cache_ttl_seconds: int = 604_800
    negative_cache_ttl_seconds: int = 3_600
    spotify_not_found_cache_ttl_seconds: int = 300
    # Expired verified matches are retained for this long and may be served
    # when a provider fails (for example, YouTube quota exhaustion).
    stale_match_grace_seconds: int = 2_592_000

    # API
    resolve_timeout_seconds: float = 30.0
    api_rate_limit_per_minute: int = 60
    trust_proxy_headers: bool = False
    cors_origins: tuple[str, ...] = ()
    log_level: str = "INFO"

    # v2: YouTube Music (ytmusicapi)
    yt_music_enabled: bool = True
    yt_music_concurrency: int = 3
    yt_music_timeout_seconds: float = 10.0
    yt_music_max_results: int = 10
    yt_music_match_threshold: float = 0.75
    yt_music_uncertain_threshold: float = 0.60
    yt_music_weight_title: float = 0.45
    yt_music_weight_artist: float = 0.35
    yt_music_weight_duration: float = 0.20

    # v2: primary resolver circuit breaker
    primary_resolver_cooldown: int = 300

    # v2: Spotify playlist extraction
    spotify_playlist_extractor: str = "auto"
    spotify_webplayer_fallback: bool = True
    spotify_embed_partial_fallback: bool = True
    spotify_playlist_page_size: int = 100
    spotify_api_max_bytes: int = 8_388_608
    spotify_bundle_max_bytes: int = 16_777_216
    spotify_webplayer_query_hash: str | None = None

    # v2: YouTube playlist extraction (yt-dlp)
    youtube_playlist_timeout_seconds: float = 90.0

    # v2: playlists and jobs
    playlist_max_tracks: int = 1000
    playlist_max_concurrent_jobs: int = 2
    playlist_max_pending_jobs: int = 50
    playlist_cache_ttl: int = 86_400
    playlist_partial_cache_ttl: int = 600
    job_ttl_seconds: int = 259_200
    job_lease_seconds: int = 120
    resolver_cache_enabled: bool = True
    v2_rate_limit_per_minute: int = 20

    def __post_init__(self) -> None:
        object.__setattr__(self, "database_path", Path(self.database_path).expanduser())
        for name, value in (
            ("HTTP_CONNECT_TIMEOUT_SECONDS", self.http_connect_timeout_seconds),
            ("HTTP_RETRY_BACKOFF_SECONDS", self.http_retry_backoff_seconds),
            ("HTTP_RETRY_AFTER_CAP_SECONDS", self.http_retry_after_cap_seconds),
            ("SPOTIFY_TIMEOUT_SECONDS", self.spotify_timeout_seconds),
            ("PROVIDER_TIMEOUT_SECONDS", self.provider_timeout_seconds),
            ("PROVIDER_RATE_LIMIT_WAIT_SECONDS", self.provider_rate_limit_wait_seconds),
            ("MATCH_THRESHOLD", self.match_threshold),
            ("WEIGHT_TITLE", self.weight_title),
            ("WEIGHT_ARTIST", self.weight_artist),
            ("WEIGHT_DURATION", self.weight_duration),
            ("OFFICIAL_BONUS", self.official_bonus),
            ("MIN_TITLE_SCORE", self.min_title_score),
            ("MIN_ARTIST_SCORE", self.min_artist_score),
            ("NO_DURATION_SCORE_CAP", self.no_duration_score_cap),
            ("SEARCH_EXTRA_QUERY_MARGIN", self.search_extra_query_margin),
            ("RESOLVE_TIMEOUT_SECONDS", self.resolve_timeout_seconds),
        ):
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{name} must be finite")
        if not 0 <= self.match_threshold <= 1:
            raise ValueError("MATCH_THRESHOLD must be between 0 and 1")
        if not 0 <= self.min_title_score <= 1:
            raise ValueError("MIN_TITLE_SCORE must be between 0 and 1")
        if not 0 <= self.min_artist_score <= 1:
            raise ValueError("MIN_ARTIST_SCORE must be between 0 and 1")
        if not 0 <= self.no_duration_score_cap <= 1:
            raise ValueError("NO_DURATION_SCORE_CAP must be between 0 and 1")
        if not 0 <= self.search_extra_query_margin <= 1:
            raise ValueError("SEARCH_EXTRA_QUERY_MARGIN must be between 0 and 1")
        if min(self.weight_title, self.weight_artist, self.weight_duration) < 0:
            raise ValueError("Matching weights cannot be negative")
        if self.weight_title + self.weight_artist <= 0:
            raise ValueError("WEIGHT_TITLE + WEIGHT_ARTIST must be positive")
        if self.weight_title + self.weight_artist + self.weight_duration <= 0:
            raise ValueError("At least one matching weight must be positive")
        if self.official_bonus < 0:
            raise ValueError("OFFICIAL_BONUS cannot be negative")
        for name, value in (
            ("HTTP_CONNECT_TIMEOUT_SECONDS", self.http_connect_timeout_seconds),
            ("SPOTIFY_TIMEOUT_SECONDS", self.spotify_timeout_seconds),
            ("PROVIDER_TIMEOUT_SECONDS", self.provider_timeout_seconds),
            ("RESOLVE_TIMEOUT_SECONDS", self.resolve_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        for name, value in (
            ("SPOTIFY_MAX_BYTES", self.spotify_max_bytes),
            ("PROVIDER_MAX_BYTES", self.provider_max_bytes),
            ("YOUTUBE_MAX_RESULTS", self.youtube_max_results),
            ("SOUNDCLOUD_MAX_RESULTS", self.soundcloud_max_results),
            ("YOUTUBE_MAX_QUERIES", self.youtube_max_queries),
            ("SOUNDCLOUD_MAX_QUERIES", self.soundcloud_max_queries),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        for name, value in (
            ("MATCH_CACHE_TTL_SECONDS", self.match_cache_ttl_seconds),
            ("METADATA_CACHE_TTL_SECONDS", self.metadata_cache_ttl_seconds),
            ("NEGATIVE_CACHE_TTL_SECONDS", self.negative_cache_ttl_seconds),
            (
                "SPOTIFY_NOT_FOUND_CACHE_TTL_SECONDS",
                self.spotify_not_found_cache_ttl_seconds,
            ),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.http_max_retries < 0:
            raise ValueError("HTTP_MAX_RETRIES cannot be negative")
        if self.spotify_max_redirects < 0:
            raise ValueError("SPOTIFY_MAX_REDIRECTS cannot be negative")
        if self.youtube_max_duration_diff_ms < 0:
            raise ValueError("YOUTUBE_MAX_DURATION_DIFF_MS cannot be negative")
        if self.soundcloud_max_duration_diff_ms < 0:
            raise ValueError("SOUNDCLOUD_MAX_DURATION_DIFF_MS cannot be negative")
        if self.api_rate_limit_per_minute < 0:
            raise ValueError("API_RATE_LIMIT_PER_MINUTE cannot be negative")
        if self.youtube_rate_limit_per_minute < 0:
            raise ValueError("YOUTUBE_RATE_LIMIT_PER_MINUTE cannot be negative")
        if self.youtube_rate_limit_burst < 0:
            raise ValueError("YOUTUBE_RATE_LIMIT_BURST cannot be negative")
        if self.soundcloud_rate_limit_per_minute < 0:
            raise ValueError("SOUNDCLOUD_RATE_LIMIT_PER_MINUTE cannot be negative")
        if self.soundcloud_rate_limit_burst < 0:
            raise ValueError("SOUNDCLOUD_RATE_LIMIT_BURST cannot be negative")
        if self.http_retry_after_cap_seconds < 0:
            raise ValueError("HTTP_RETRY_AFTER_CAP_SECONDS cannot be negative")
        if self.http_retry_backoff_seconds < 0:
            raise ValueError("HTTP_RETRY_BACKOFF_SECONDS cannot be negative")
        if self.provider_rate_limit_wait_seconds < 0:
            raise ValueError("PROVIDER_RATE_LIMIT_WAIT_SECONDS cannot be negative")
        if self.stale_match_grace_seconds < 0:
            raise ValueError("STALE_MATCH_GRACE_SECONDS cannot be negative")
        self._validate_v2()

    def _validate_v2(self) -> None:
        for name, value in (
            ("YT_MUSIC_MATCH_THRESHOLD", self.yt_music_match_threshold),
            ("YT_MUSIC_UNCERTAIN_THRESHOLD", self.yt_music_uncertain_threshold),
        ):
            if not (math.isfinite(value) and 0 <= value <= 1):
                raise ValueError(f"{name} must be between 0 and 1")
        if self.yt_music_uncertain_threshold > self.yt_music_match_threshold:
            raise ValueError(
                "YT_MUSIC_UNCERTAIN_THRESHOLD cannot exceed YT_MUSIC_MATCH_THRESHOLD"
            )
        weights = (
            self.yt_music_weight_title,
            self.yt_music_weight_artist,
            self.yt_music_weight_duration,
        )
        if min(weights) < 0 or self.yt_music_weight_title + self.yt_music_weight_artist <= 0:
            raise ValueError("YT_MUSIC_WEIGHT_* must be non-negative with title+artist > 0")
        for name, value in (
            ("YT_MUSIC_TIMEOUT_SECONDS", self.yt_music_timeout_seconds),
            ("YOUTUBE_PLAYLIST_TIMEOUT_SECONDS", self.youtube_playlist_timeout_seconds),
        ):
            if not (math.isfinite(value) and value > 0):
                raise ValueError(f"{name} must be greater than zero")
        for name, value in (
            ("YT_MUSIC_CONCURRENCY", self.yt_music_concurrency),
            ("YT_MUSIC_MAX_RESULTS", self.yt_music_max_results),
            ("SPOTIFY_PLAYLIST_PAGE_SIZE", self.spotify_playlist_page_size),
            ("SPOTIFY_API_MAX_BYTES", self.spotify_api_max_bytes),
            ("SPOTIFY_BUNDLE_MAX_BYTES", self.spotify_bundle_max_bytes),
            ("PLAYLIST_MAX_TRACKS", self.playlist_max_tracks),
            ("PLAYLIST_MAX_CONCURRENT_JOBS", self.playlist_max_concurrent_jobs),
            ("PLAYLIST_MAX_PENDING_JOBS", self.playlist_max_pending_jobs),
            ("PLAYLIST_CACHE_TTL", self.playlist_cache_ttl),
            ("PLAYLIST_PARTIAL_CACHE_TTL", self.playlist_partial_cache_ttl),
            ("JOB_TTL_SECONDS", self.job_ttl_seconds),
            ("JOB_LEASE_SECONDS", self.job_lease_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.spotify_playlist_page_size > 500:
            raise ValueError("SPOTIFY_PLAYLIST_PAGE_SIZE cannot exceed 500")
        if self.primary_resolver_cooldown < 0:
            raise ValueError("PRIMARY_RESOLVER_COOLDOWN cannot be negative")
        if self.v2_rate_limit_per_minute < 0:
            raise ValueError("V2_RATE_LIMIT_PER_MINUTE cannot be negative")
        if self.spotify_playlist_extractor not in {
            "auto",
            "spotify_downloader",
            "webplayer",
        }:
            raise ValueError(
                "SPOTIFY_PLAYLIST_EXTRACTOR must be auto, spotify_downloader or webplayer"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        return cls(
            database_path=Path(_text(source, "DATABASE_PATH", "data/resolver.db")),
            http_user_agent=_text(source, "HTTP_USER_AGENT", "spotilink/0.1"),
            http_connect_timeout_seconds=_number(
                source, "HTTP_CONNECT_TIMEOUT_SECONDS", 5.0
            ),
            http_max_retries=_integer(source, "HTTP_MAX_RETRIES", 1),
            http_retry_backoff_seconds=_number(
                source, "HTTP_RETRY_BACKOFF_SECONDS", 0.5
            ),
            http_retry_after_cap_seconds=_number(
                source, "HTTP_RETRY_AFTER_CAP_SECONDS", 2.0
            ),
            spotify_timeout_seconds=_number(source, "SPOTIFY_TIMEOUT_SECONDS", 10.0),
            spotify_max_bytes=_integer(source, "SPOTIFY_MAX_BYTES", 2_097_152),
            spotify_max_redirects=_integer(source, "SPOTIFY_MAX_REDIRECTS", 3),
            youtube_api_key=_optional(source, "YOUTUBE_API_KEY"),
            youtube_max_results=_integer(source, "YOUTUBE_MAX_RESULTS", 10),
            youtube_max_queries=_integer(source, "YOUTUBE_MAX_QUERIES", 1),
            youtube_rate_limit_per_minute=_integer(
                source, "YOUTUBE_RATE_LIMIT_PER_MINUTE", 10
            ),
            youtube_rate_limit_burst=_integer(source, "YOUTUBE_RATE_LIMIT_BURST", 2),
            soundcloud_client_id=_optional(source, "SOUNDCLOUD_CLIENT_ID"),
            soundcloud_client_secret=_optional(source, "SOUNDCLOUD_CLIENT_SECRET"),
            soundcloud_max_results=_integer(source, "SOUNDCLOUD_MAX_RESULTS", 20),
            soundcloud_max_queries=_integer(source, "SOUNDCLOUD_MAX_QUERIES", 2),
            soundcloud_rate_limit_per_minute=_integer(
                source, "SOUNDCLOUD_RATE_LIMIT_PER_MINUTE", 30
            ),
            soundcloud_rate_limit_burst=_integer(
                source, "SOUNDCLOUD_RATE_LIMIT_BURST", 2
            ),
            provider_timeout_seconds=_number(source, "PROVIDER_TIMEOUT_SECONDS", 8.0),
            provider_max_bytes=_integer(source, "PROVIDER_MAX_BYTES", 1_048_576),
            provider_rate_limit_wait_seconds=_number(
                source, "PROVIDER_RATE_LIMIT_WAIT_SECONDS", 2.0
            ),
            match_threshold=_number(source, "MATCH_THRESHOLD", 0.80),
            weight_title=_number(source, "WEIGHT_TITLE", 0.50),
            weight_artist=_number(source, "WEIGHT_ARTIST", 0.35),
            weight_duration=_number(source, "WEIGHT_DURATION", 0.15),
            official_bonus=_number(source, "OFFICIAL_BONUS", 0.06),
            min_title_score=_number(source, "MIN_TITLE_SCORE", 0.65),
            min_artist_score=_number(source, "MIN_ARTIST_SCORE", 0.50),
            no_duration_score_cap=_number(source, "NO_DURATION_SCORE_CAP", 0.85),
            require_duration=_boolean(source, "REQUIRE_DURATION", True),
            search_extra_query_margin=_number(
                source, "SEARCH_EXTRA_QUERY_MARGIN", 0.10
            ),
            youtube_max_duration_diff_ms=_integer(
                source, "YOUTUBE_MAX_DURATION_DIFF_MS", 30_000
            ),
            soundcloud_max_duration_diff_ms=_integer(
                source, "SOUNDCLOUD_MAX_DURATION_DIFF_MS", 15_000
            ),
            match_cache_ttl_seconds=_integer(
                source, "MATCH_CACHE_TTL_SECONDS", 604_800
            ),
            metadata_cache_ttl_seconds=_integer(
                source, "METADATA_CACHE_TTL_SECONDS", 604_800
            ),
            negative_cache_ttl_seconds=_integer(
                source, "NEGATIVE_CACHE_TTL_SECONDS", 3_600
            ),
            spotify_not_found_cache_ttl_seconds=_integer(
                source, "SPOTIFY_NOT_FOUND_CACHE_TTL_SECONDS", 300
            ),
            stale_match_grace_seconds=_integer(
                source, "STALE_MATCH_GRACE_SECONDS", 2_592_000
            ),
            resolve_timeout_seconds=_number(source, "RESOLVE_TIMEOUT_SECONDS", 30.0),
            api_rate_limit_per_minute=_integer(source, "API_RATE_LIMIT_PER_MINUTE", 60),
            trust_proxy_headers=_boolean(source, "TRUST_PROXY_HEADERS", False),
            cors_origins=_csv(source, "CORS_ORIGINS", ()),
            log_level=_text(source, "LOG_LEVEL", "INFO").upper(),
            yt_music_enabled=_boolean(source, "YT_MUSIC_ENABLED", True),
            yt_music_concurrency=_integer(source, "YT_MUSIC_CONCURRENCY", 3),
            yt_music_timeout_seconds=_number(source, "YT_MUSIC_TIMEOUT_SECONDS", 10.0),
            yt_music_max_results=_integer(source, "YT_MUSIC_MAX_RESULTS", 10),
            yt_music_match_threshold=_number(source, "YT_MUSIC_MATCH_THRESHOLD", 0.75),
            yt_music_uncertain_threshold=_number(
                source, "YT_MUSIC_UNCERTAIN_THRESHOLD", 0.60
            ),
            yt_music_weight_title=_number(source, "YT_MUSIC_WEIGHT_TITLE", 0.45),
            yt_music_weight_artist=_number(source, "YT_MUSIC_WEIGHT_ARTIST", 0.35),
            yt_music_weight_duration=_number(source, "YT_MUSIC_WEIGHT_DURATION", 0.20),
            primary_resolver_cooldown=_integer(source, "PRIMARY_RESOLVER_COOLDOWN", 300),
            spotify_playlist_extractor=_text(
                source, "SPOTIFY_PLAYLIST_EXTRACTOR", "auto"
            ).lower(),
            spotify_webplayer_fallback=_boolean(
                source, "SPOTIFY_WEBPLAYER_FALLBACK", True
            ),
            spotify_embed_partial_fallback=_boolean(
                source, "SPOTIFY_EMBED_PARTIAL_FALLBACK", True
            ),
            spotify_playlist_page_size=_integer(source, "SPOTIFY_PLAYLIST_PAGE_SIZE", 100),
            spotify_api_max_bytes=_integer(source, "SPOTIFY_API_MAX_BYTES", 8_388_608),
            spotify_bundle_max_bytes=_integer(
                source, "SPOTIFY_BUNDLE_MAX_BYTES", 16_777_216
            ),
            spotify_webplayer_query_hash=_optional(
                source, "SPOTIFY_WEBPLAYER_QUERY_HASH"
            ),
            youtube_playlist_timeout_seconds=_number(
                source, "YOUTUBE_PLAYLIST_TIMEOUT_SECONDS", 90.0
            ),
            playlist_max_tracks=_integer(source, "PLAYLIST_MAX_TRACKS", 1000),
            playlist_max_concurrent_jobs=_integer(
                source, "PLAYLIST_MAX_CONCURRENT_JOBS", 2
            ),
            playlist_max_pending_jobs=_integer(source, "PLAYLIST_MAX_PENDING_JOBS", 50),
            playlist_cache_ttl=_integer(source, "PLAYLIST_CACHE_TTL", 86_400),
            playlist_partial_cache_ttl=_integer(source, "PLAYLIST_PARTIAL_CACHE_TTL", 600),
            job_ttl_seconds=_integer(source, "JOB_TTL_SECONDS", 259_200),
            job_lease_seconds=_integer(source, "JOB_LEASE_SECONDS", 120),
            resolver_cache_enabled=_boolean(source, "RESOLVER_CACHE_ENABLED", True),
            v2_rate_limit_per_minute=_integer(source, "V2_RATE_LIMIT_PER_MINUTE", 20),
        )
