"""Mongo-слой: upsert/дедупликация, статусы, выборки, health источников, kv."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.storage.db import Database
from tests.conftest import make_job, seed


class TestUpsert:
    async def test_insert_then_update(self, db: Database) -> None:
        job = make_job()

        db_id, is_new = await db.upsert_job(job)

        assert is_new is True
        assert db_id > 0

        _, is_new_again = await db.upsert_job(job)
        assert is_new_again is False

    async def test_second_source_appends_without_new_row(self, db: Database) -> None:
        await db.upsert_job(make_job(source="gh", sources=["gh"]))

        await db.upsert_job(make_job(source="remoteok", sources=["remoteok"]))

        row = await db.get_job(make_job().id)
        assert row is not None
        assert row["sources"] == ["gh", "remoteok"]
        assert await db.count_jobs() == 1

    async def test_update_preserves_status_and_first_seen(self, db: Database) -> None:
        job = make_job()
        await db.upsert_job(job)
        await db.set_status(job.id, "applied")
        old_first_seen = (await db.get_job(job.id))["first_seen_at"]

        refreshed = make_job(status="new", score=99.0)
        await db.upsert_job(refreshed)

        row = await db.get_job(job.id)
        assert row["status"] == "applied"
        assert row["first_seen_at"] == old_first_seen
        assert row["score"] == 99.0

    async def test_canonical_url_is_stored(self, db: Database) -> None:
        await db.upsert_job(make_job(url="https://a.io/jobs/1?utm_source=tg"))

        row = await db.get_job(make_job().id)

        assert row["canonical_url"] == "https://a.io/jobs/1"


class TestStatuses:
    async def test_set_status_returns_false_for_unknown_id(self, db: Database) -> None:
        assert await db.set_status("nope", "saved") is False

    async def test_applied_sets_followup_clock(self, db: Database) -> None:
        job_id = await seed(db)

        assert await db.set_status(job_id, "applied") is True

        row = await db.get_job(job_id)
        assert row["status"] == "applied"
        assert row["followup_at"] is not None

    async def test_rejected_stores_reason(self, db: Database) -> None:
        job_id = await seed(db)

        await db.set_status(job_id, "rejected", reason="низкая вилка")

        row = await db.get_job(job_id)
        assert row["reject_reason"] == "низкая вилка"

    async def test_followup_due_only_after_days(self, db: Database) -> None:
        job_id = await seed(db)
        await db.set_status(job_id, "applied")

        assert await db.followup_due(days=6) == []

        # followup_due(days=6) means "applied at least 6 days ago".
        await db.set_followup(job_id, days=-7)
        due = await db.followup_due(days=6)

        assert [row["id"] for row in due] == [job_id]

    async def test_mark_notified(self, db: Database) -> None:
        job_id = await seed(db)

        await db.mark_notified([job_id])

        row = await db.get_job(job_id)
        assert row["notified_at"] is not None
        # Empty list must be a no-op, not a SQL error.
        await db.mark_notified([])


class TestListJobs:
    async def test_orders_by_score_desc(self, db: Database) -> None:
        low = await seed(db, title="DevOps A", score=30.0)
        high = await seed(db, title="DevOps B", url="https://b.io/2", score=80.0)

        rows = await db.list_jobs()

        assert [row["id"] for row in rows] == [high, low]

    async def test_filter_by_status(self, db: Database) -> None:
        saved = await seed(db, title="DevOps A")
        await seed(db, title="DevOps B", url="https://b.io/2")
        await db.set_status(saved, "saved")

        rows = await db.list_jobs(statuses=["saved"])

        assert [row["id"] for row in rows] == [saved]

    async def test_filter_by_min_score(self, db: Database) -> None:
        await seed(db, title="DevOps A", score=20.0)
        strong = await seed(db, title="DevOps B", url="https://b.io/2", score=90.0)

        rows = await db.list_jobs(min_score=50.0)

        assert [row["id"] for row in rows] == [strong]

    async def test_only_unnotified(self, db: Database) -> None:
        notified = await seed(db, title="DevOps A")
        pending = await seed(db, title="DevOps B", url="https://b.io/2")
        await db.mark_notified([notified])

        rows = await db.list_jobs(only_unnotified=True)

        assert [row["id"] for row in rows] == [pending]

    async def test_suspicious_flag(self, db: Database) -> None:
        suspect = await seed(db, title="DevOps A", suspicious=True)
        await seed(db, title="DevOps B", url="https://b.io/2")

        only_suspects = await db.list_jobs(include_suspicious=True)
        without = await db.list_jobs(include_suspicious=False)

        assert [row["id"] for row in only_suspects] == [suspect]
        assert len(without) == 1

    async def test_pagination(self, db: Database) -> None:
        for i in range(3):
            await seed(db, title=f"DevOps {i}", url=f"https://b.io/{i}", score=50.0 + i)

        page = await db.list_jobs(limit=2, offset=2)

        assert len(page) == 1

    async def test_since_days_excludes_old(self, db: Database) -> None:
        # ``upsert_job`` always stamps first_seen_at=now on INSERT, so the stale row
        # is produced the way a backfill would: by rewriting the field itself.
        old = make_job(title="DevOps old", url="https://old.io/1")
        await db.upsert_job(old)
        long_ago = (datetime.now(tz=UTC) - timedelta(days=40)).isoformat(timespec="seconds")
        await db.set_first_seen(old.id, long_ago)
        fresh = await seed(db, title="DevOps fresh", url="https://fresh.io/1")

        rows = await db.list_jobs(since_days=7)

        assert [row["id"] for row in rows] == [fresh]


class TestAnalytics:
    async def test_funnel_counts(self, db: Database) -> None:
        first = await seed(db, title="DevOps A")
        await seed(db, title="DevOps B", url="https://b.io/2")
        await db.set_status(first, "applied")

        funnel = await db.funnel_counts()

        assert funnel == {"new": 1, "applied": 1}

    async def test_top_sources(self, db: Database) -> None:
        await seed(db, title="DevOps A", source="gh")
        await seed(db, title="DevOps B", url="https://b.io/2", source="gh")
        await seed(db, title="DevOps C", url="https://c.io/3", source="hh")

        top = await db.top_sources()

        assert top[0][0] == "gh"
        assert top[0][1] == 2

    async def test_events_are_counted(self, db: Database) -> None:
        await db.log_event("abc", "card_sent", "score=70")
        await db.log_event("abc", "card_sent")
        await db.log_event(None, "run")

        assert await db.count_events("card_sent", since_days=1) == 2
        assert await db.count_events("missing", since_days=1) == 0

    async def test_reject_tallies(self, db: Database) -> None:
        first = await seed(db, title="DevOps A")
        second = await seed(db, title="DevOps B", url="https://b.io/2")
        await db.set_status(first, "rejected", reason="стек не тот")
        await db.set_status(second, "rejected", reason="стек не тот")

        assert await db.company_reject_tally() == {"CryptoCo": 2}
        assert await db.reject_reason_tally() == {"стек не тот": 2}


class TestSourceHealth:
    async def test_ok_then_error(self, db: Database) -> None:
        await db.record_source_ok("gh", 12)

        health = await db.source_health_all()
        assert [h.name for h in health] == ["gh"]
        assert health[0].ok is True
        assert health[0].jobs_found == 12

        await db.record_source_error("gh", "TimeoutError: boom")

        health = await db.source_health_all()
        assert health[0].ok is False
        assert health[0].consecutive_errors == 1
        assert "boom" in health[0].last_error

    async def test_recovery_resets_errors(self, db: Database) -> None:
        await db.record_source_error("hh", "HTTP 500")
        await db.record_source_ok("hh", 5)

        health = await db.source_health_all()
        assert health[0].ok is True
        assert health[0].consecutive_errors == 0
        assert health[0].runs == 2


class TestKeyValue:
    async def test_default_when_absent(self, db: Database) -> None:
        assert await db.kv_get("nope", "[]") == "[]"

    async def test_set_and_get(self, db: Database) -> None:
        await db.kv_set("blocklist", '["Acme"]')

        assert await db.kv_get("blocklist") == '["Acme"]'

    async def test_overwrite(self, db: Database) -> None:
        await db.kv_set("k", "one")
        await db.kv_set("k", "two")

        assert await db.kv_get("k") == "two"


class TestLlmAccounting:
    async def test_zero_on_start(self, db: Database) -> None:
        assert await db.llm_spend_today() == 0.0

    async def test_usage_accumulates(self, db: Database) -> None:
        await db.llm_add_usage(1, 100, 50, 0.01)
        await db.llm_add_usage(2, 200, 90, 0.02)

        assert await db.llm_spend_today() == pytest.approx(0.03, abs=1e-9)


class TestBackup:
    async def test_creates_file(self, db: Database, tmp_path: Any) -> None:
        await seed(db)

        path = await db.backup(tmp_path / "backups", keep=3)

        assert path is not None
        assert path.exists()
        assert path.stat().st_size > 0

    async def test_rotation_keeps_limit(self, db: Database, tmp_path: Any) -> None:
        await seed(db)
        backups_dir = tmp_path / "backups"

        for _ in range(5):
            await db.backup(backups_dir, keep=2)

        assert len(list(backups_dir.glob("*.json"))) <= 2


class TestLifecycle:
    async def test_reopen_keeps_data(self, tmp_path: Any) -> None:
        from mongomock_motor import AsyncMongoMockClient

        # One shared in-memory store stands in for a persistent server.
        client = AsyncMongoMockClient()

        store = Database(client=client, db_name="reopen")
        await store.connect()
        job = make_job()
        await store.upsert_job(job)
        await store.close()

        again = Database(client=client, db_name="reopen")
        await again.connect()
        row = await again.get_job(job.id)
        await again.close()

        assert row is not None
        assert row["company"] == "CryptoCo"
