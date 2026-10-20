from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import InboundRateLimitMiddleware, install_exception_handlers, router
from .cache import Cache
from .concurrency import SlidingWindowLimiter
from .config import Settings
from .http import LimitedHttpClient
from .providers.base import Provider
from .providers.soundcloud import SoundCloudProvider
from .providers.youtube import YouTubeProvider
from .service import ResolverService
from .spotify import SpotifyClient

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    providers: Mapping[str, Provider] | None = None,
    clock: Callable[[], float] | None = None,
    http_sleep: Callable[[float], Awaitable[None]] | None = None,
) -> FastAPI:
    app_settings = settings or Settings.from_env()
    epoch_clock = clock or time.time

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _configure_logging(app_settings)
        cache = Cache(app_settings.database_path)
        await cache.initialize()
        try:
            await cache.prune(epoch_clock())
        except Exception:
            logger.warning("Initial cache prune failed", exc_info=True)

        async def discard_cookie_header(request: httpx.Request) -> None:
            request.headers.pop("cookie", None)

        raw_http = httpx.AsyncClient(
            transport=transport,
            timeout=None,
            headers={"User-Agent": app_settings.http_user_agent},
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30.0,
            ),
            follow_redirects=False,
            event_hooks={"request": [discard_cookie_header]},
        )

        async def discard_response_cookies(response: httpx.Response) -> None:
            raw_http.cookies.clear()

        # The service never uses cookies; clear any jar entries immediately so
        # anonymous Spotify session cookies cannot accumulate or be replayed.
        raw_http.event_hooks["response"].append(discard_response_cookies)
        http = LimitedHttpClient(
            raw_http,
            max_retries=app_settings.http_max_retries,
            retry_backoff_seconds=app_settings.http_retry_backoff_seconds,
            retry_after_cap_seconds=app_settings.http_retry_after_cap_seconds,
            connect_timeout_seconds=app_settings.http_connect_timeout_seconds,
            sleep=http_sleep,
        )
        spotify = SpotifyClient(http, app_settings)

        if providers is None:
            provider_map: dict[str, Provider] = {
                "youtube": YouTubeProvider(http, app_settings),
                "soundcloud": SoundCloudProvider(http, app_settings, clock=epoch_clock),
            }
        else:
            provider_map = dict(providers)

        service = ResolverService(
            app_settings,
            spotify,
            cache,
            provider_map,
            clock=epoch_clock,
        )
        app.state.raw_http = raw_http
        app.state.http = http
        app.state.spotify = spotify
        app.state.cache = cache
        app.state.providers = provider_map
        app.state.service = service
        try:
            yield
        finally:
            await http.aclose()
            await cache.close()

    app = FastAPI(
        title="Spotify Resolver",
        version="0.1.0",
        description=(
            "Resolve public Spotify track links to verified YouTube or SoundCloud "
            "track links. Spotify is read from public track pages without the Spotify "
            "Web API; no audio is downloaded or proxied."
        ),
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.inbound_limiter = SlidingWindowLimiter(
        app_settings.api_rate_limit_per_minute
    )

    app.include_router(router)
    install_exception_handlers(app)

    # Added before CORS so CORS is the outermost middleware for browser clients.
    app.add_middleware(InboundRateLimitMiddleware)
    if app_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(app_settings.cors_origins),
            allow_methods=["GET", "OPTIONS"],
            allow_headers=["*"],
            max_age=600,
        )
    return app


def _configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def run() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional convenience dependency
    pass
else:
    load_dotenv()


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    run()
