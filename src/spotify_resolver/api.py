from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .errors import ResolverError
from .models import PLATFORMS
from .schemas import ResolveResponse, response_from_error, response_from_result

logger = logging.getLogger(__name__)
router = APIRouter()
_SHOWCASE_PAGE = Path(__file__).resolve().parent / "static" / "index.html"


@router.get(
    "/v1/resolve",
    response_model=ResolveResponse,
    response_model_exclude_none=False,
    summary="Resolve a Spotify track URL to a verified provider track",
    responses={
        200: {
            "description": (
                "Verified match, stale cached match, or an unverified search fallback"
            )
        },
        400: {"description": "Missing or invalid query parameter"},
        404: {"description": "Spotify track does not exist"},
        422: {"description": "Unsupported Spotify URL"},
        429: {"description": "Inbound API rate limit exceeded"},
        502: {"description": "Spotify metadata is unavailable"},
        503: {"description": "Provider unavailable, rate-limited, or not configured"},
        504: {"description": "Spotify or resolution timeout"},
    },
)
async def resolve_track(
    request: Request,
    url: str | None = Query(
        default=None, description="A public open.spotify.com track URL"
    ),
    platform: str = Query(default="youtube", description="youtube or soundcloud"),
    fallback: bool = Query(
        default=True, description="Allow the other platform as fallback"
    ),
) -> ResolveResponse:
    if url is None or not url.strip():
        raise ResolverError(
            "missing_url",
            "The 'url' query parameter is required.",
            status_code=400,
        )
    requested_platform = (platform or "").strip().lower()
    if requested_platform not in PLATFORMS:
        raise ResolverError(
            "invalid_platform",
            "The 'platform' parameter must be 'youtube' or 'soundcloud'.",
            status_code=400,
        )

    service = request.app.state.service
    settings = request.app.state.settings
    try:
        result = await asyncio.wait_for(
            service.resolve(url, requested_platform, fallback),
            timeout=settings.resolve_timeout_seconds,
        )
    except TimeoutError as exc:
        raise ResolverError(
            "resolution_timeout",
            "Resolution exceeded the configured timeout.",
            status_code=504,
            retryable=True,
        ) from exc
    return response_from_result(result)


@router.get("/healthz", summary="Liveness and configuration health")
async def healthz(request: Request) -> JSONResponse:
    cache = request.app.state.cache
    providers = request.app.state.providers
    settings = request.app.state.settings
    database_ok = await cache.ping()
    configured = {
        name: bool(getattr(provider, "configured", False))
        for name, provider in sorted(providers.items())
    }
    any_provider = any(configured.values())
    healthy = database_ok and any_provider
    body = {
        "status": "ok" if healthy else "degraded",
        "database": "ok" if database_ok else "unavailable",
        "providers": configured,
        "matching_threshold": settings.match_threshold,
    }
    return JSONResponse(status_code=200 if healthy else 503, content=body)


@router.get("/", include_in_schema=False, response_class=HTMLResponse)
async def root() -> HTMLResponse:
    return HTMLResponse(
        content=_SHOWCASE_PAGE.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


class InboundRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method != "GET" or request.url.path != "/v1/resolve":
            return await call_next(request)

        limiter = getattr(request.app.state, "inbound_limiter", None)
        if limiter is None or not limiter.enabled:
            return await call_next(request)

        key = client_key(
            request, trust_proxy=request.app.state.settings.trust_proxy_headers
        )
        retry_after = limiter.check(key)
        if retry_after is None:
            return await call_next(request)

        error = ResolverError(
            "rate_limited",
            "Too many resolution requests from this client.",
            status_code=429,
            retryable=True,
            retry_after_seconds=retry_after,
        )
        payload = response_from_error(error)
        return JSONResponse(
            status_code=429,
            content=payload.model_dump(mode="json"),
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )


def client_key(request: Request, *, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def install_exception_handlers(app: Any) -> None:
    @app.exception_handler(ResolverError)
    async def resolver_error_handler(
        request: Request,
        exc: ResolverError,
    ) -> JSONResponse:
        payload = response_from_error(exc)
        headers: dict[str, str] = {}
        if exc.retry_after_seconds is not None:
            headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after_seconds)))
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(mode="json"),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        error = ResolverError(
            "invalid_query_parameter",
            "One or more query parameters are invalid.",
            status_code=400,
        )
        payload = response_from_error(error)
        return JSONResponse(
            status_code=400,
            content=payload.model_dump(mode="json"),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        if exc.status_code == 404:
            code = "not_found"
            message = "The requested endpoint does not exist."
        elif exc.status_code == 405:
            code = "method_not_allowed"
            message = "The HTTP method is not allowed for this endpoint."
        else:
            code = "http_error"
            message = "The request could not be completed."
        error = ResolverError(code, message, status_code=exc.status_code)
        payload = response_from_error(error)
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.exception(
            "Unhandled API error for %s %s", request.method, request.url.path
        )
        error = ResolverError(
            "internal_error",
            "An unexpected error occurred.",
            status_code=500,
        )
        payload = response_from_error(error)
        return JSONResponse(
            status_code=500,
            content=payload.model_dump(mode="json"),
        )
