from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


class HttpError(Exception):
    pass


class HttpStatusError(HttpError):
    def __init__(
        self,
        status_code: int,
        *,
        retryable: bool,
        retry_after: float | None = None,
        body: bytes = b"",
    ) -> None:
        super().__init__(f"Upstream returned HTTP {status_code}")
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.body = body


class HttpTimeoutError(HttpError):
    pass


class HttpNetworkError(HttpError):
    pass


class HttpResponseTooLarge(HttpError):
    pass


class HttpRedirectError(HttpError):
    pass


class HttpDecodeError(HttpError):
    pass


class LimitedHttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.5,
        retry_after_cap_seconds: float = 2.0,
        connect_timeout_seconds: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_after_cap_seconds = retry_after_cap_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._sleep = sleep or asyncio.sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_text(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout: float,
        allowed_hosts: set[str] | frozenset[str],
        max_redirects: int = 0,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> str:
        body = await self._request_bytes(
            "GET",
            url,
            max_bytes=max_bytes,
            timeout=timeout,
            allowed_hosts=allowed_hosts,
            max_redirects=max_redirects,
            headers=headers,
            params=params,
        )
        encoding = "utf-8"
        return body.decode(encoding, errors="replace")

    async def get_json(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout: float,
        allowed_hosts: set[str] | frozenset[str],
        max_redirects: int = 0,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        body = await self._request_bytes(
            "GET",
            url,
            max_bytes=max_bytes,
            timeout=timeout,
            allowed_hosts=allowed_hosts,
            max_redirects=max_redirects,
            headers=headers,
            params=params,
        )
        return self._decode_json(body)

    async def post_form_json(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        auth: tuple[str, str],
        max_bytes: int,
        timeout: float,
        allowed_hosts: set[str] | frozenset[str],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        body = await self._request_bytes(
            "POST",
            url,
            max_bytes=max_bytes,
            timeout=timeout,
            allowed_hosts=allowed_hosts,
            max_redirects=0,
            headers=headers,
            data=data,
            auth=auth,
        )
        return self._decode_json(body)

    @staticmethod
    def _decode_json(body: bytes) -> Any:
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise HttpDecodeError("Upstream returned malformed JSON") from exc

    async def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        timeout: float,
        allowed_hosts: set[str] | frozenset[str],
        max_redirects: int,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> bytes:
        attempt = 0
        while True:
            try:
                return await self._single_request(
                    method,
                    url,
                    max_bytes=max_bytes,
                    timeout=timeout,
                    allowed_hosts=allowed_hosts,
                    max_redirects=max_redirects,
                    headers=headers,
                    params=params,
                    data=data,
                    auth=auth,
                )
            except HttpStatusError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                attempt += 1
                await self._sleep(self._retry_delay(attempt, exc.retry_after))

    async def _single_request(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        timeout: float,
        allowed_hosts: set[str] | frozenset[str],
        max_redirects: int,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> bytes:
        current_url = url
        redirects = 0
        while True:
            self._validate_url(current_url, allowed_hosts)
            request_headers = dict(headers or {})
            try:
                async with self._client.stream(
                    method,
                    current_url,
                    params=params,
                    headers=request_headers or None,
                    data=data,
                    auth=auth,
                    timeout=httpx.Timeout(
                        timeout,
                        connect=min(timeout, self._connect_timeout_seconds),
                    ),
                    follow_redirects=False,
                ) as response:
                    status = response.status_code

                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise HttpStatusError(status, retryable=False)
                        if redirects >= max_redirects:
                            raise HttpRedirectError("Too many upstream redirects")
                        next_url = urljoin(current_url, location)
                        self._validate_url(next_url, allowed_hosts)
                        current_url = next_url
                        redirects += 1
                        continue

                    if 200 <= status < 300:
                        content_length = response.headers.get("content-length")
                        if (
                            content_length
                            and content_length.isdigit()
                            and int(content_length) > max_bytes
                        ):
                            raise HttpResponseTooLarge(
                                "Upstream response exceeded the size limit"
                            )
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(chunks) + len(chunk) > max_bytes:
                                raise HttpResponseTooLarge(
                                    "Upstream response exceeded the size limit"
                                )
                            chunks.extend(chunk)
                        return bytes(chunks)

                    body = await self._read_limited(response, 65_536)
                    retry_after = _parse_retry_after(
                        response.headers.get("retry-after")
                    )
                    raise HttpStatusError(
                        status,
                        retryable=status == 429 or status >= 500,
                        retry_after=retry_after,
                        body=body,
                    )
            except httpx.TimeoutException as exc:
                raise HttpTimeoutError("Upstream request timed out") from exc
            except httpx.HTTPError as exc:
                raise HttpNetworkError("Upstream network request failed") from exc

    @staticmethod
    async def _read_limited(response: httpx.Response, limit: int) -> bytes:
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            if len(chunks) + len(chunk) > limit:
                break
            chunks.extend(chunk)
        return bytes(chunks)

    @staticmethod
    def _validate_url(url: str, allowed_hosts: set[str] | frozenset[str]) -> None:
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise HttpRedirectError("Upstream URL is malformed") from exc
        if parsed.scheme != "https":
            raise HttpRedirectError("Upstream URL must use HTTPS")
        if not parsed.hostname or parsed.hostname.lower() not in allowed_hosts:
            raise HttpRedirectError("Upstream URL is not on an allowed host")

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = self._retry_backoff_seconds * (2 ** (attempt - 1))
        if self._retry_after_cap_seconds >= 0:
            delay = min(delay, self._retry_after_cap_seconds)
        return max(0.0, delay)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        if math.isfinite(seconds):
            return max(0.0, seconds)
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
