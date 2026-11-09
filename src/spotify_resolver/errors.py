from __future__ import annotations

from .models import Attempt, SearchFallback, SpotifyTrack


class InvalidSpotifyUrl(ValueError):
    """The input is not a supported Spotify track URL."""


class ResolverError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 500,
        retryable: bool = False,
        track: SpotifyTrack | None = None,
        attempts: tuple[Attempt, ...] = (),
        search_fallback: SearchFallback | None = None,
        retry_after_seconds: float | None = None,
        metadata_cached: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.track = track
        self.attempts = attempts
        self.search_fallback = search_fallback
        self.retry_after_seconds = retry_after_seconds
        self.metadata_cached = metadata_cached


class ProviderError(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ProviderNotConfigured(ProviderError):
    def __init__(self, message: str = "Provider is not configured") -> None:
        super().__init__("not_configured", message)


class ProviderTimeout(ProviderError):
    def __init__(self, message: str = "Provider request timed out") -> None:
        super().__init__("timeout", message)


class ProviderRateLimited(ProviderError):
    def __init__(
        self,
        message: str = "Provider rate limit exceeded",
        *,
        upstream: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__("rate_limited", message)
        # True when the upstream service (not a local token bucket) refused
        # the request because of quota exhaustion or rate limiting.
        self.upstream = upstream
        self.retry_after_seconds = retry_after_seconds


class ProviderAuthError(ProviderError):
    def __init__(self, message: str = "Provider rejected its credentials") -> None:
        super().__init__("auth_error", message)


class ProviderUpstreamError(ProviderError):
    def __init__(self, message: str = "Provider request failed") -> None:
        super().__init__("upstream_error", message)


class SpotifyNotFound(Exception):
    pass


class SpotifyTimeout(Exception):
    pass


class SpotifyRateLimited(Exception):
    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("Spotify rate limited the page request")
        self.retry_after_seconds = retry_after_seconds


class SpotifyUnavailable(Exception):
    pass


class SpotifyMetadataError(Exception):
    pass
