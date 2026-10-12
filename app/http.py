"""Shared async HTTP client: timeouts, exponential-backoff retries, per-domain
rate limiting and ETag/If-Modified-Since caching.

No source may hammer a domain: requests to the same host are spaced by
``domain_min_interval`` seconds.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class CacheEntry:
    etag: str | None
    last_modified: str | None
    body: str


class HttpFetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        domain_min_interval: float = 1.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.5",
            },
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._max_retries = max(1, max_retries)
        self._min_interval = domain_min_interval
        self._last_hit: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._cache: dict[str, CacheEntry] = {}
        #: number of 304 responses served from cache (useful for /sources)
        self.cache_hits = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _host(url: str) -> str:
        return urlsplit(url).netloc

    async def _throttle(self, url: str) -> None:
        host = self._host(url)
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last_hit.get(host, 0.0)
            wait = self._min_interval - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit[host] = time.monotonic()

    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
    ) -> tuple[str | None, int]:
        """Return ``(body, status)``. ``(None, 304)`` means "not modified"."""
        request_headers = dict(headers or {})
        entry = self._cache.get(url) if use_cache else None
        if entry:
            if entry.etag:
                request_headers["If-None-Match"] = entry.etag
            if entry.last_modified:
                request_headers["If-Modified-Since"] = entry.last_modified

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            await self._throttle(url)
            try:
                resp = await self._client.get(url, headers=request_headers)
            except httpx.HTTPError as exc:  # network-level: retry
                last_error = exc
                backoff = 0.5 * (2**attempt)
                log.warning(
                    "http error url=%s attempt=%d err=%s retry_in=%.1fs",
                    url, attempt + 1, type(exc).__name__, backoff,
                )
                await asyncio.sleep(backoff)
                continue

            if resp.status_code == 304 and entry:
                self.cache_hits += 1
                return entry.body, 304
            if resp.status_code >= 500:
                last_error = httpx.HTTPStatusError(
                    "server error", request=resp.request, response=resp
                )
                backoff = 0.5 * (2**attempt)
                log.warning(
                    "http %d url=%s attempt=%d retry_in=%.1fs",
                    resp.status_code, url, attempt + 1, backoff,
                )
                await asyncio.sleep(backoff)
                continue
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2") or 2)
                log.warning("http 429 url=%s waiting=%.1fs", url, retry_after)
                await asyncio.sleep(min(retry_after, 30.0))
                continue
            if resp.status_code >= 400:
                log.warning("http %d url=%s (no retry)", resp.status_code, url)
                return None, resp.status_code

            if use_cache:
                self._cache[url] = CacheEntry(
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                    body=resp.text,
                )
            return resp.text, resp.status_code

        log.error("http exhausted retries url=%s err=%s", url, last_error)
        return None, 0

    async def get_json(self, url: str, **kwargs: object) -> tuple[object | None, int]:
        import json

        body, status = await self.get_text(url, **kwargs)  # type: ignore[arg-type]
        if body is None:
            return None, status
        try:
            return json.loads(body), status
        except json.JSONDecodeError as exc:
            log.error("json decode failed url=%s err=%s", url, exc)
            return None, status
