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
    database = Database(tmp_path / "web.db")  # type: ignore[operator]
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
        db_path=str(tmp_path / "unused.db"),  # type: ignore[operator]
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
async def test_web_token_gate(db: Database, tmp_path: object) -> None:
    settings = Settings(
        telegram_enabled=False,
        db_path=str(tmp_path / "unused.db"),
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
