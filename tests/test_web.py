"""Web UI: healthz, listing, status transitions, optional token gate."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.storage import Database
from app.web import create_web_app
from tests.conftest import make_job


@pytest.fixture
async def db(tmp_path: object) -> Database:
    database = Database("mongomock://", db_name="jobhunter_web")
    await database.connect()
    job = make_job(
        company="Reelful",
        title="founding full-stack engineer",
        url="https://t.me/hrlunapark/408",
        score=42,
        raw={"channel": "hrlunapark", "post": "hrlunapark/408"},
    )
    await database.upsert_job(job)
    yield database
    await database.close()


@pytest.fixture
def settings(tmp_path: object) -> Settings:
    return Settings(
        telegram_enabled=False,
        mongo_uri="mongomock://",
        web_enabled=True,
        web_token="",
    )


@pytest.mark.asyncio
async def test_healthz_and_index(db: Database, settings: Settings) -> None:
    app = create_web_app(db=db, settings=settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.text == "ok"

        page = await client.get("/")
        assert page.status_code == 200
        assert "founding full-stack engineer" in page.text
        assert "Reelful" in page.text
        assert "@hrlunapark" in page.text

        # HTML number inputs submit ``min_score=`` when empty — must not 422.
        blank = await client.get("/", params={"min_score": "", "q": "", "status": ""})
        assert blank.status_code == 200


@pytest.mark.asyncio
async def test_channel_filter_and_status_change(db: Database, settings: Settings) -> None:
    app = create_web_app(db=db, settings=settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        filtered = await client.get("/api/jobs", params={"channel": "hrlunapark"})
        assert filtered.status_code == 200
        body = filtered.json()
        assert body["total"] == 1
        job_id = body["items"][0]["id"]

        miss = await client.get("/api/jobs", params={"channel": "missing"})
        assert miss.json()["total"] == 0

        resp = await client.post(
            f"/jobs/{job_id}/status",
            data={"status": "saved", "next": f"/jobs/{job_id}"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        row = await db.get_job(job_id)
        assert row is not None
        assert row["status"] == "saved"


@pytest.mark.asyncio
async def test_feed_buttons_return_to_the_feed(db: Database, settings: Settings) -> None:
    """★/✓/✕ post an absolute ``next`` — the redirect must stay on the feed.

    Regression: the old handler only accepted targets starting with ``/``, so
    every button bounced to the vacancy card and the list looked dead.
    """
    app = create_web_app(db=db, settings=settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page = await client.get("/", params={"page": 1, "q": ""})
        assert page.status_code == 200
        # The form must post a *relative* target, never an off-site one.
        assert 'name="next" value="/?' in page.text

        job_id = (await client.get("/api/jobs")).json()["items"][0]["id"]
        resp = await client.post(
            f"/jobs/{job_id}/status",
            data={"status": "ignored", "reason": "other", "next": "http://test:80/?page=1&q="},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?page=1&q="
        assert (await db.get_job(job_id))["status"] == "ignored"

        off_site = await client.post(
            f"/jobs/{job_id}/status",
            data={"status": "saved", "next": "https://evil.example/x"},
            follow_redirects=False,
        )
        assert off_site.headers["location"] == f"/jobs/{job_id}"


@pytest.mark.asyncio
async def test_archived_rows_leave_the_default_feed(db: Database, settings: Settings) -> None:
    """The feed hides the archive; ``?status=archived`` still reaches it."""
    from datetime import UTC, datetime, timedelta

    stale = make_job(
        company="OldCo",
        title="legacy platform engineer",
        url="https://t.me/hrlunapark/9",
        score=60,
        posted_at=datetime.now(tz=UTC) - timedelta(days=200),
    )
    await db.upsert_job(stale)

    app = create_web_app(db=db, settings=settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await db.archive_stale(days=90)) == 1

        page = await client.get("/")
        assert "legacy platform engineer" not in page.text
        assert "founding full-stack engineer" in page.text

        api = await client.get("/api/jobs")
        assert api.json()["total"] == 1

        in_archive = await client.get("/", params={"status": "archived"})
        assert "legacy platform engineer" in in_archive.text
        assert "founding full-stack engineer" not in in_archive.text


@pytest.mark.asyncio
async def test_dismissed_row_leaves_the_default_feed(db: Database, settings: Settings) -> None:
    """``✕`` must make the row disappear on reload, not just relabel it.

    Regression: the redirect worked, but ``ignored`` rows stayed in the default
    feed, so the button looked like nothing happened.
    """
    app = create_web_app(db=db, settings=settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        job_id = (await client.get("/api/jobs")).json()["items"][0]["id"]

        await client.post(
            f"/jobs/{job_id}/status",
            data={"status": "ignored", "reason": "other", "next": "/"},
            follow_redirects=False,
        )

        assert "founding full-stack engineer" not in (await client.get("/")).text
        assert (await client.get("/api/jobs")).json()["total"] == 0

        # Still reachable when asked for explicitly, and still counted in the funnel.
        ignored = await client.get("/", params={"status": "ignored"})
        assert "founding full-stack engineer" in ignored.text
        funnel = (await client.get("/api/stats")).json()["funnel"]
        assert funnel["ignored"] == 1


@pytest.mark.asyncio
async def test_web_token_gate(db: Database, tmp_path: object) -> None:
    settings = Settings(
        telegram_enabled=False,
        mongo_uri="mongomock://",
        web_token="secret",
    )
    app = create_web_app(db=db, settings=settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/")
        assert denied.status_code == 401

        ok = await client.get("/", params={"token": "secret"}, follow_redirects=False)
        assert ok.status_code == 303
        assert "jh_token" in ok.cookies
