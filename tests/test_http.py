"""``HttpFetcher``: ETag cache, retries, 429 handling, per-domain throttle (ТЗ §3)."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from app.http import HttpFetcher

URL = "https://api.example.com/jobs"


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff sleeps would make the retry tests needlessly slow.

    ``app.http`` holds a plain ``import asyncio``, so this patches the shared
    module attribute; the throttle tests deliberately skip this fixture.
    """

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("app.http.asyncio.sleep", instant)


def make_http(
    handler: Any,
    *,
    retries: int = 3,
    interval: float = 0.0,
    agent: str = "jobhunter-tests/1.0",
) -> HttpFetcher:
    return HttpFetcher(
        user_agent=agent,
        timeout=5.0,
        max_retries=retries,
        domain_min_interval=interval,
        transport=httpx.MockTransport(handler),
    )


class TestHappyPath:
    async def test_returns_body_and_status(self) -> None:
        http = make_http(lambda request: httpx.Response(200, text="hello"))

        body, status = await http.get_text(URL)

        assert (body, status) == ("hello", 200)
        await http.aclose()

    async def test_sends_configured_user_agent(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="ok")

        http = make_http(handler, agent="jobhunter-ua/2.0")
        await http.get_text(URL)

        assert seen[0].headers["User-Agent"] == "jobhunter-ua/2.0"
        await http.aclose()

    async def test_extra_headers_are_forwarded(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="ok")

        http = make_http(handler)
        await http.get_text(URL, headers={"HH-User-Agent": "custom/1.0"})

        assert seen[0].headers["HH-User-Agent"] == "custom/1.0"
        await http.aclose()


class TestConditionalCache:
    async def test_etag_is_replayed_and_304_serves_cache(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if "If-None-Match" in request.headers:
                return httpx.Response(304)
            return httpx.Response(200, text="payload", headers={"ETag": '"abc"'})

        http = make_http(handler)

        assert await http.get_text(URL) == ("payload", 200)
        assert await http.get_text(URL) == ("payload", 304)

        assert "If-None-Match" not in seen[0].headers
        assert seen[1].headers["If-None-Match"] == '"abc"'
        assert http.cache_hits == 1
        await http.aclose()

    async def test_last_modified_is_replayed(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if "If-Modified-Since" in request.headers:
                return httpx.Response(304)
            return httpx.Response(
                200, text="v1", headers={"Last-Modified": "Wed, 01 Oct 2026 00:00:00 GMT"}
            )

        http = make_http(handler)
        await http.get_text(URL)
        body, status = await http.get_text(URL)

        assert (body, status) == ("v1", 304)
        assert seen[1].headers["If-Modified-Since"].startswith("Wed, 01 Oct 2026")
        await http.aclose()

    async def test_fresh_200_replaces_cached_entry(self) -> None:
        seen: list[httpx.Request] = []
        responses: list[httpx.Response] = [
            httpx.Response(200, text="v1", headers={"ETag": '"1"'}),
            httpx.Response(200, text="v2", headers={"ETag": '"2"'}),
            httpx.Response(304),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return responses.pop(0)

        http = make_http(handler)

        assert await http.get_text(URL) == ("v1", 200)
        assert await http.get_text(URL) == ("v2", 200)  # a fresh 200 wins over the cache
        assert await http.get_text(URL) == ("v2", 304)  # the cache now holds v2

        assert seen[1].headers["If-None-Match"] == '"1"'
        assert seen[2].headers["If-None-Match"] == '"2"'
        assert http.cache_hits == 1
        await http.aclose()

    async def test_use_cache_false_sends_no_conditional_headers(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="x", headers={"ETag": '"1"'})

        http = make_http(handler)
        await http.get_text(URL, use_cache=False)
        await http.get_text(URL, use_cache=False)

        assert "If-None-Match" not in seen[1].headers
        assert http.cache_hits == 0
        await http.aclose()

    async def test_304_without_cache_entry_is_returned_as_is(self) -> None:
        # A bare 304 with nothing cached cannot be answered from the cache.
        http = make_http(lambda request: httpx.Response(304))

        assert await http.get_text(URL) == ("", 304)
        assert http.cache_hits == 0
        await http.aclose()


class TestRetries:
    async def test_4xx_is_not_retried(self, no_sleep: None) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(403)

        http = make_http(handler, retries=3)
        body, status = await http.get_text(URL)

        assert (body, status) == (None, 403)
        assert calls == 1
        await http.aclose()

    async def test_5xx_retries_then_gives_up_with_status_zero(self, no_sleep: None) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503)

        http = make_http(handler, retries=3)
        body, status = await http.get_text(URL)

        assert (body, status) == (None, 0)
        assert calls == 3
        await http.aclose()

    async def test_5xx_then_success(self, no_sleep: None) -> None:
        responses = [httpx.Response(500), httpx.Response(200, text="late ok")]

        def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        http = make_http(handler, retries=3)

        assert await http.get_text(URL) == ("late ok", 200)
        await http.aclose()

    async def test_network_error_is_retried(self, no_sleep: None) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("boom", request=request)

        http = make_http(handler, retries=2)
        body, status = await http.get_text(URL)

        assert (body, status) == (None, 0)
        assert calls == 2
        await http.aclose()

    async def test_429_waits_and_retries(self, no_sleep: None) -> None:
        responses = [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, text="after limit"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        http = make_http(handler, retries=3)

        assert await http.get_text(URL) == ("after limit", 200)
        await http.aclose()

    async def test_429_retry_after_is_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr("app.http.asyncio.sleep", record)

        responses: list[httpx.Response] = [
            httpx.Response(429, headers={"Retry-After": "600"}),
            httpx.Response(200, text="ok"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return responses.pop(0)

        http = make_http(handler, retries=3)

        assert await http.get_text(URL) == ("ok", 200)
        assert slept == [30.0]  # min(600, 30) — a huge Retry-After cannot stall the run
        await http.aclose()


class TestThrottle:
    async def test_same_domain_is_spaced_apart(self) -> None:
        http = make_http(lambda request: httpx.Response(200, text="ok"), interval=0.2)

        started = time.monotonic()
        await http.get_text(URL)
        await http.get_text(URL)
        elapsed = time.monotonic() - started

        assert elapsed >= 0.2
        await http.aclose()

    async def test_different_domains_are_not_blocked_by_each_other(self) -> None:
        http = make_http(lambda request: httpx.Response(200, text="ok"), interval=0.2)

        started = time.monotonic()
        await http.get_text("https://a.example.com/1")
        await http.get_text("https://b.example.com/1")
        elapsed = time.monotonic() - started

        assert elapsed < 0.2
        await http.aclose()


class TestGetJson:
    async def test_parses_json(self) -> None:
        http = make_http(lambda request: httpx.Response(200, text='{"jobs": [1, 2]}'))

        payload, status = await http.get_json(URL)

        assert payload == {"jobs": [1, 2]}
        assert status == 200
        await http.aclose()

    async def test_invalid_json_returns_none_with_status(self) -> None:
        http = make_http(lambda request: httpx.Response(200, text="<html>nope</html>"))

        payload, status = await http.get_json(URL)

        assert payload is None
        assert status == 200
        await http.aclose()

    async def test_http_failure_propagates(self) -> None:
        http = make_http(lambda request: httpx.Response(404))

        assert await http.get_json(URL) == (None, 404)
        await http.aclose()
