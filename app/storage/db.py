"""MongoDB storage (motor) with a home-grown version table.

Kept deliberately simple: one ``jobs`` collection keyed by a stable content hash,
an ``events`` append-log for the funnel, ``source_health`` for the /sources view,
and a ``kv`` collection for runtime overrides the bot can change.

The store runs in MongoDB (see ``docker-compose.yml``) so the WAL/bind-mount
corruption that SQLite hit on Docker Desktop for macOS cannot come back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import PyMongoError

from app.logging_setup import get_logger
from app.models import Job, canonical_url

log = get_logger(__name__)

SCHEMA_VERSION = 2

#: Indexes created on connect: ``{collection: [(keys, options), ...]}``.
#: ``canonical_url`` stays unique — dedup relies on it.
_INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict[str, Any]]]] = {
    "jobs": [
        ([("status", ASCENDING)], {}),
        ([("score", DESCENDING), ("first_seen_at", DESCENDING)], {}),
        ([("first_seen_at", DESCENDING)], {}),
        ([("source", ASCENDING)], {}),
        ([("canonical_url", ASCENDING)], {"unique": True}),
    ],
    "events": [
        ([("type", ASCENDING), ("ts", DESCENDING)], {}),
    ],
}

_MONGO_MOCK_SCHEME = "mongomock://"


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _since_iso(days: int) -> str:
    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def _regex(needle: str) -> re.Pattern[str]:
    """Case-insensitive substring match, anchored by ``re.escape``."""
    return re.compile(re.escape(needle), re.IGNORECASE)


@dataclass(slots=True)
class SourceHealth:
    name: str
    last_ok_at: str | None
    last_error_at: str | None
    last_error: str
    consecutive_errors: int
    jobs_found: int
    runs: int

    @property
    def ok(self) -> bool:
        # Timestamps only have second granularity, so comparing them can hide an
        # error logged in the same second as the previous success. The counter is
        # authoritative: ``record_source_ok`` resets it, ``record_source_error``
        # increments it.
        return self.consecutive_errors == 0


class Database:
    """Async MongoDB facade.

    ``uri`` is a Mongo connection string. The ``mongomock://`` scheme swaps in an
    in-memory client so the test-suite does not need a running server.
    """

    def __init__(
        self,
        uri: str | None = None,
        *,
        db_name: str = "jobhunter",
        client: Any | None = None,
    ) -> None:
        self.uri = uri or "mongodb://localhost:27017"
        self.db_name = db_name
        #: Optional pre-built client: lets tests share one in-memory store
        #: across ``Database`` instances (the "reopen keeps data" case).
        self._injected = client
        self._client: Any | None = None
        self._db: Any | None = None

    async def connect(self) -> None:
        self._client = self._injected or self._build_client()
        self._db = self._client[self.db_name]
        await self._create_indexes()
        await self._migrate()

    def _build_client(self) -> Any:
        if self.uri.startswith(_MONGO_MOCK_SCHEME):  # tests / offline smoke run
            from mongomock_motor import AsyncMongoMockClient

            return AsyncMongoMockClient()
        return _motor_client(self.uri)

    async def _create_indexes(self) -> None:
        for name, specs in _INDEXES.items():
            for keys, options in specs:
                await self.db[name].create_index(keys, **options)
        await self.db["source_health"].create_index([("name", ASCENDING)], unique=True)

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("Database.connect() was not awaited")
        return self._client

    @property
    def db(self) -> Any:
        if self._db is None:
            raise RuntimeError("Database.connect() was not awaited")
        return self._db

    async def _migrate(self) -> None:
        row = await self.db["meta"].find_one({"_id": "schema_version"})
        current = int(row["version"]) if row else 0
        if current < SCHEMA_VERSION:
            await self.db["meta"].update_one(
                {"_id": "schema_version"},
                {"$set": {"version": SCHEMA_VERSION, "applied_at": now_iso()}},
                upsert=True,
            )
            log.info("applied schema version %d (was %d)", SCHEMA_VERSION, current)

    async def close(self) -> None:
        if self._client is None:
            return
        # An injected client is owned by the caller (tests share one store).
        if self._injected is None:
            self._client.close()
        self._client = None
        self._db = None

    # ------------------------------------------------------------------ jobs

    async def _next_seq(self) -> int:
        doc = await self.db["counters"].find_one_and_update(
            {"_id": "jobs"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc["seq"]) if doc else 1

    @staticmethod
    def _to_doc(job: Job, *, seq: int, first_seen: str) -> dict[str, Any]:
        return {
            "_id": job.id,
            "id": job.id,
            "seq": seq,
            "source": job.source,
            "sources": list(job.sources or [job.source]),
            "company": job.company,
            "title": job.title,
            "url": job.url,
            "canonical_url": canonical_url(job.url) or job.id,
            "location": job.location,
            "remote": job.remote,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "currency": job.currency,
            "description": job.description,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "first_seen_at": first_seen,
            "last_seen_at": now_iso(),
            "raw": dict(job.raw),
            "status": job.status,
            "score": job.score,
            "score_breakdown": [list(item) for item in job.score_breakdown],
            "flags": list(job.flags),
            "geo_restricted": bool(job.geo_restricted),
            "suspicious": bool(job.suspicious),
            "llm_verdict": job.llm_verdict,
            "llm_summary": job.llm_summary,
            "notified_at": job.notified_at.isoformat() if job.notified_at else None,
            "followup_at": job.followup_at.isoformat() if job.followup_at else None,
            "reject_reason": "",
            "updated_at": now_iso(),
        }

    async def upsert_job(self, job: Job) -> tuple[int, bool]:
        """Insert or refresh. Returns ``(db_id, is_new)``.

        Dedup happens here: an existing document keeps its status, first_seen_at
        and notified_at; only freshness fields are updated. A second source for
        the same vacancy appends to ``sources`` instead of creating a new card.
        """
        existing = await self.get_job(job.id)
        if existing is None:
            seq = await self._next_seq()
            doc = self._to_doc(job, seq=seq, first_seen=now_iso())
            try:
                await self.db["jobs"].insert_one(doc)
            except PyMongoError as exc:  # unique canonical_url race
                log.warning("insert failed job=%s: %s", job.id, exc)
                existing = await self.get_job(job.id)
                if existing is None:
                    raise
                return int(existing.get("seq", 0)), False
            return seq, True

        merged = list(existing.get("sources") or []) + list(job.sources or [job.source])
        sources = sorted(set(merged))
        await self.db["jobs"].update_one(
            {"_id": job.id},
            {
                "$set": {
                    "sources": sources,
                    "url": job.url,
                    "location": job.location,
                    "remote": job.remote,
                    "salary_min": job.salary_min,
                    "salary_max": job.salary_max,
                    "currency": job.currency,
                    "description": job.description,
                    "posted_at": (
                        job.posted_at.isoformat() if job.posted_at else existing.get("posted_at")
                    ),
                    "last_seen_at": now_iso(),
                    "raw": dict(job.raw),
                    "score": job.score,
                    "score_breakdown": [list(item) for item in job.score_breakdown],
                    "flags": list(job.flags),
                    "geo_restricted": bool(job.geo_restricted),
                    "suspicious": bool(job.suspicious),
                    "llm_verdict": job.llm_verdict,
                    "llm_summary": job.llm_summary,
                    "updated_at": now_iso(),
                }
            },
        )
        return int(existing.get("seq", 0)), False

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return await self.db["jobs"].find_one({"_id": job_id})

    async def set_first_seen(self, job_id: str, value: str) -> None:
        """Rewrite ``first_seen_at`` (used by backfills and by the tests)."""
        await self.db["jobs"].update_one({"_id": job_id}, {"$set": {"first_seen_at": value}})

    async def set_status(self, job_id: str, status: str, *, reason: str = "") -> bool:
        res = await self.db["jobs"].update_one(
            {"_id": job_id},
            {
                "$set": {
                    "status": status,
                    "reject_reason": reason,
                    "followup_at": now_iso() if status == "applied" else None,
                    "updated_at": now_iso(),
                }
            },
        )
        return res.matched_count > 0

    async def set_followup(self, job_id: str, *, days: int = 0) -> None:
        """Push (or reset) the follow-up reminder clock."""
        when = datetime.now(tz=UTC) + timedelta(days=days)
        await self.db["jobs"].update_one(
            {"_id": job_id},
            {"$set": {"followup_at": when.isoformat(timespec="seconds"), "updated_at": now_iso()}},
        )

    async def mark_notified(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        stamp = now_iso()
        await self.db["jobs"].update_many(
            {"_id": {"$in": list(job_ids)}},
            {"$set": {"notified_at": stamp, "updated_at": stamp}},
        )

    async def set_llm(self, job_id: str, verdict: str, summary: str) -> None:
        await self.db["jobs"].update_one(
            {"_id": job_id},
            {"$set": {"llm_verdict": verdict, "llm_summary": summary, "updated_at": now_iso()}},
        )

    # ----------------------------------------------------------------- queries

    @staticmethod
    def _filter(
        *,
        statuses: list[str] | None = None,
        min_score: float | None = None,
        since_days: int | None = None,
        only_unnotified: bool = False,
        include_suspicious: bool | None = None,
        q: str | None = None,
        source: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if statuses:
            query["status"] = {"$in": list(statuses)}
        if min_score is not None:
            query["score"] = {"$gte": min_score}
        if since_days is not None:
            query["first_seen_at"] = {"$gte": _since_iso(since_days)}
        if only_unnotified:
            # ``None`` also matches documents where the field is absent, exactly
            # like SQL ``IS NULL``.
            query["notified_at"] = None
        if include_suspicious is not None:
            query["suspicious"] = bool(include_suspicious)
        if source:
            query["source"] = source
        if channel:
            # Telegram posts store ``{"channel": "hrlunapark", ...}`` in ``raw``.
            query["raw.channel"] = channel.strip().lstrip("@")
        if q:
            needle = _regex(q.strip())
            query["$or"] = [
                {"title": needle},
                {"company": needle},
                {"description": needle},
                {"url": needle},
            ]
        return query

    async def list_jobs(
        self,
        *,
        statuses: list[str] | None = None,
        min_score: float | None = None,
        since_days: int | None = None,
        only_unnotified: bool = False,
        include_suspicious: bool | None = None,
        q: str | None = None,
        source: str | None = None,
        channel: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = self._filter(
            statuses=statuses,
            min_score=min_score,
            since_days=since_days,
            only_unnotified=only_unnotified,
            include_suspicious=include_suspicious,
            q=q,
            source=source,
            channel=channel,
        )
        cursor = (
            self.db["jobs"]
            .find(query)
            .sort([("score", DESCENDING), ("first_seen_at", DESCENDING)])
            .skip(offset)
            .limit(limit)
        )
        return await cursor.to_list(length=limit)

    async def count_jobs(self, **kwargs: Any) -> int:
        kwargs.pop("limit", None)
        kwargs.pop("offset", None)
        kwargs.pop("only_unnotified", None)
        rows = await self.list_jobs(**kwargs, limit=10_000)
        return len(rows)

    async def followup_due(self, days: int = 6) -> list[dict[str, Any]]:
        cutoff = _since_iso(days)
        cursor = self.db["jobs"].find(
            {
                "status": "applied",
                "followup_at": {"$ne": None, "$lte": cutoff},
            }
        ).sort([("followup_at", ASCENDING)])
        return await cursor.to_list(length=1000)

    async def funnel_counts(self, since_days: int | None = None) -> dict[str, int]:
        match: dict[str, Any] = {}
        if since_days is not None:
            match["first_seen_at"] = {"$gte": _since_iso(since_days)}
        pipeline = [
            {"$match": match},
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        ]
        out: dict[str, int] = {}
        async for row in self.db["jobs"].aggregate(pipeline):
            out[str(row["_id"])] = int(row["n"])
        return out

    async def top_sources(self, since_days: int = 30) -> list[tuple[str, int, float]]:
        pipeline = [
            {"$match": {"first_seen_at": {"$gte": _since_iso(since_days)}}},
            {
                "$group": {
                    "_id": "$source",
                    "n": {"$sum": 1},
                    "avg_score": {"$avg": "$score"},
                }
            },
            {"$sort": {"n": -1}},
            {"$limit": 10},
        ]
        out: list[tuple[str, int, float]] = []
        async for row in self.db["jobs"].aggregate(pipeline):
            out.append((str(row["_id"]), int(row["n"]), float(row.get("avg_score") or 0.0)))
        return out

    # ---------------------------------------------------------------- events

    async def log_event(self, job_id: str | None, type_: str, payload: str = "") -> None:
        await self.db["events"].insert_one(
            {"job_id": job_id, "ts": now_iso(), "type": type_, "payload": payload}
        )

    async def count_events(self, type_: str, since_days: int) -> int:
        return int(
            await self.db["events"].count_documents(
                {"type": type_, "ts": {"$gte": _since_iso(since_days)}}
            )
        )

    async def _tally(self, field: str, *, cutoff: str, extra: dict[str, Any]) -> dict[str, int]:
        match: dict[str, Any] = {"status": "rejected", "updated_at": {"$gte": cutoff}, **extra}
        pipeline = [
            {"$match": match},
            {"$group": {"_id": f"${field}", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": 50},
        ]
        out: dict[str, int] = {}
        async for row in self.db["jobs"].aggregate(pipeline):
            out[str(row["_id"])] = int(row["n"])
        return out

    async def company_reject_tally(self, since_days: int = 30) -> dict[str, int]:
        """Companies the owner rejected — feeds the scoring feedback loop."""
        return await self._tally("company", cutoff=_since_iso(since_days), extra={})

    async def reject_reason_tally(self, since_days: int = 30) -> dict[str, int]:
        return await self._tally(
            "reject_reason",
            cutoff=_since_iso(since_days),
            extra={"reject_reason": {"$nin": ["", None]}},
        )

    # --------------------------------------------------------- source health

    async def record_source_ok(self, name: str, jobs_found: int) -> None:
        await self.db["source_health"].update_one(
            {"name": name},
            {
                "$set": {
                    "name": name,
                    "last_ok_at": now_iso(),
                    "last_error_at": None,
                    "last_error": "",
                    "consecutive_errors": 0,
                    "jobs_found": jobs_found,
                },
                "$inc": {"runs": 1},
            },
            upsert=True,
        )

    async def record_source_error(self, name: str, error: str) -> None:
        current = await self.db["source_health"].find_one({"name": name})
        errors = int(current.get("consecutive_errors", 0) if current else 0) + 1
        await self.db["source_health"].update_one(
            {"name": name},
            {
                "$set": {
                    "name": name,
                    "last_error_at": now_iso(),
                    "last_error": error[:200],
                    "consecutive_errors": errors,
                },
                "$inc": {"runs": 1},
            },
            upsert=True,
        )

    async def source_health_all(self) -> list[SourceHealth]:
        cursor = self.db["source_health"].find({}).sort([("name", ASCENDING)])
        return [
            SourceHealth(
                name=r["name"],
                last_ok_at=r.get("last_ok_at"),
                last_error_at=r.get("last_error_at"),
                last_error=r.get("last_error", ""),
                consecutive_errors=int(r.get("consecutive_errors", 0)),
                jobs_found=int(r.get("jobs_found", 0)),
                runs=int(r.get("runs", 0)),
            )
            async for r in cursor
        ]

    # -------------------------------------------------------------------- kv

    async def kv_get(self, key: str, default: str = "") -> str:
        row = await self.db["kv"].find_one({"_id": key})
        return str(row["value"]) if row else default

    async def kv_set(self, key: str, value: str) -> None:
        await self.db["kv"].update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)

    # ------------------------------------------------------------- llm usage

    async def llm_spend_today(self) -> float:
        day = datetime.now(tz=UTC).date().isoformat()
        row = await self.db["llm_usage"].find_one({"_id": day})
        return float(row["cost_usd"]) if row else 0.0

    async def llm_add_usage(self, calls: int, in_tok: int, out_tok: int, cost: float) -> None:
        day = datetime.now(tz=UTC).date().isoformat()
        await self.db["llm_usage"].update_one(
            {"_id": day},
            {
                "$inc": {
                    "calls": calls,
                    "in_tokens": in_tok,
                    "out_tokens": out_tok,
                    "cost_usd": cost,
                }
            },
            upsert=True,
        )

    # ---------------------------------------------------------------- backup

    async def backup(self, backup_dir: str | Path, keep: int = 7) -> Path | None:
        """Daily JSON snapshot of every collection, with rotation.

        ``mongodump`` is not shipped in the runtime image, so the snapshot is
        written through the driver: it stays consistent (no file copy of a live
        store) and is readable without extra tooling.
        """
        import json

        target = Path(backup_dir)
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        dest = target / f"jobhunter-{stamp}.json"

        snapshot: dict[str, list[dict[str, Any]]] = {}
        for name in ("jobs", "events", "source_health", "kv", "llm_usage", "meta", "counters"):
            snapshot[name] = await self.db[name].find({}).to_list(length=1_000_000)
        dest.write_text(json.dumps(snapshot, ensure_ascii=False, default=str), encoding="utf-8")

        backups = sorted(target.glob("jobhunter-*.json"))
        for old in backups[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        log.info("backup written %s (keeping %d)", dest.name, keep)
        return dest


def _motor_client(uri: str) -> Any:
    """Import motor lazily so the mock path does not need a server connection."""
    from motor.motor_asyncio import AsyncIOMotorClient

    return AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
