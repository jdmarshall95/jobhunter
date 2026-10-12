"""SQLite storage (aiosqlite) with a home-grown version table.

Kept deliberately simple: one ``jobs`` table keyed by a stable content hash, an
``events`` append-log for the funnel, ``source_health`` for the /sources view,
and a ``kv`` table for runtime overrides the bot can change.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from app.logging_setup import get_logger
from app.models import Job, canonical_url

log = get_logger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    sources         TEXT NOT NULL DEFAULT '[]',
    company         TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    canonical_url   TEXT NOT NULL,
    location        TEXT NOT NULL DEFAULT '',
    remote          INTEGER,
    salary_min      REAL,
    salary_max      REAL,
    currency        TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    posted_at       TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    raw             TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'new',
    score           REAL NOT NULL DEFAULT 0,
    score_breakdown TEXT NOT NULL DEFAULT '[]',
    flags           TEXT NOT NULL DEFAULT '[]',
    geo_restricted  INTEGER NOT NULL DEFAULT 0,
    suspicious      INTEGER NOT NULL DEFAULT 0,
    llm_verdict     TEXT NOT NULL DEFAULT '',
    llm_summary     TEXT NOT NULL DEFAULT '',
    notified_at     TEXT,
    followup_at     TEXT,
    reject_reason   TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_jobs_seen   ON jobs(first_seen_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_canon ON jobs(canonical_url);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);

CREATE TABLE IF NOT EXISTS source_health (
    name               TEXT PRIMARY KEY,
    last_ok_at         TEXT,
    last_error_at      TEXT,
    last_error         TEXT NOT NULL DEFAULT '',
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    jobs_found         INTEGER NOT NULL DEFAULT 0,
    runs               INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day        TEXT PRIMARY KEY,
    calls      INTEGER NOT NULL DEFAULT 0,
    in_tokens  INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


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
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._migrate()
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() was not awaited")
        return self._db

    async def _migrate(self) -> None:
        try:
            await self.db.executescript(_SCHEMA)
        except sqlite3.OperationalError as exc:
            log.error("schema creation failed: %s", exc)
            raise
        cur = await self.db.execute("SELECT MAX(version) AS v FROM schema_version")
        row = await cur.fetchone()
        current = row["v"] if row and row["v"] is not None else 0
        if current < SCHEMA_VERSION:
            await self.db.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, now_iso()),
            )
            log.info("applied schema version %d (was %d)", SCHEMA_VERSION, current)

    # ------------------------------------------------------------------ jobs

    @staticmethod
    def _to_row(job: Job, *, first_seen: str) -> tuple:
        return (
            job.id,
            job.source,
            json.dumps(job.sources or [job.source], ensure_ascii=False),
            job.company,
            job.title,
            job.url,
            canonical_url(job.url) or job.id,
            job.location,
            None if job.remote is None else int(job.remote),
            job.salary_min,
            job.salary_max,
            job.currency,
            job.description,
            job.posted_at.isoformat() if job.posted_at else None,
            first_seen,
            now_iso(),
            json.dumps(job.raw, ensure_ascii=False, default=str),
            job.status,
            job.score,
            json.dumps(job.score_breakdown, ensure_ascii=False),
            json.dumps(job.flags, ensure_ascii=False),
            int(job.geo_restricted),
            int(job.suspicious),
            job.llm_verdict,
            job.llm_summary,
            job.notified_at.isoformat() if job.notified_at else None,
            job.followup_at.isoformat() if job.followup_at else None,
            "",
            now_iso(),
        )

    async def upsert_job(self, job: Job) -> tuple[int, bool]:
        """Insert or refresh. Returns ``(db_id, is_new)``.

        Dedup happens here: an existing row keeps its status, first_seen_at and
        notified_at; only freshness fields are updated. A second source for the
        same vacancy appends to ``sources`` instead of creating a new card.
        """
        existing = await self.get_job(job.id)
        if existing is None:
            cur = await self.db.execute(
                """INSERT INTO jobs(id, source, sources, company, title, url, canonical_url,
                       location, remote, salary_min, salary_max, currency, description, posted_at,
                       first_seen_at, last_seen_at, raw, status, score, score_breakdown, flags,
                       geo_restricted, suspicious, llm_verdict, llm_summary, notified_at,
                       followup_at, reject_reason, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                self._to_row(job, first_seen=now_iso()),
            )
            await self.db.commit()
            return int(cur.lastrowid or 0), True

        sources = sorted(set(json.loads(existing["sources"]) + (job.sources or [job.source])))
        await self.db.execute(
            """UPDATE jobs SET sources=?, url=?, location=?, remote=?, salary_min=?, salary_max=?,
                       currency=?, description=?, posted_at=?, last_seen_at=?, raw=?, score=?,
                       score_breakdown=?, flags=?, geo_restricted=?, suspicious=?, llm_verdict=?,
                       llm_summary=?, updated_at=? WHERE id=?""",
            (
                json.dumps(sources, ensure_ascii=False),
                job.url,
                job.location,
                None if job.remote is None else int(job.remote),
                job.salary_min,
                job.salary_max,
                job.currency,
                job.description,
                job.posted_at.isoformat() if job.posted_at else existing["posted_at"],
                now_iso(),
                json.dumps(job.raw, ensure_ascii=False, default=str),
                job.score,
                json.dumps(job.score_breakdown, ensure_ascii=False),
                json.dumps(job.flags, ensure_ascii=False),
                int(job.geo_restricted),
                int(job.suspicious),
                job.llm_verdict,
                job.llm_summary,
                now_iso(),
                job.id,
            ),
        )
        await self.db.commit()
        # ``sqlite3.Row`` has no key-based ``__contains__``; the keys() view does.
        keys = existing.keys()
        return int(existing["id_row"] if "id_row" in keys else 0), False

    async def get_job(self, job_id: str) -> sqlite3.Row | None:
        cur = await self.db.execute("SELECT rowid AS id_row, * FROM jobs WHERE id=?", (job_id,))
        return await cur.fetchone()

    async def set_status(self, job_id: str, status: str, *, reason: str = "") -> bool:
        cur = await self.db.execute(
            "UPDATE jobs SET status=?, reject_reason=?, followup_at=?, updated_at=? WHERE id=?",
            (
                status,
                reason,
                now_iso() if status == "applied" else None,
                now_iso(),
                job_id,
            ),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def set_followup(self, job_id: str, *, days: int = 0) -> None:
        """Push (or reset) the follow-up reminder clock."""
        when = datetime.now(tz=UTC) + timedelta(days=days)
        await self.db.execute(
            "UPDATE jobs SET followup_at=?, updated_at=? WHERE id=?",
            (when.isoformat(timespec="seconds"), now_iso(), job_id),
        )
        await self.db.commit()

    async def mark_notified(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        stamp = now_iso()
        await self.db.executemany(
            "UPDATE jobs SET notified_at=?, updated_at=? WHERE id=?",
            [(stamp, stamp, jid) for jid in job_ids],
        )
        await self.db.commit()

    async def set_llm(self, job_id: str, verdict: str, summary: str) -> None:
        await self.db.execute(
            "UPDATE jobs SET llm_verdict=?, llm_summary=?, updated_at=? WHERE id=?",
            (verdict, summary, now_iso(), job_id),
        )
        await self.db.commit()

    async def list_jobs(
        self,
        *,
        statuses: list[str] | None = None,
        min_score: float | None = None,
        since_days: int | None = None,
        only_unnotified: bool = False,
        include_suspicious: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        where: list[str] = []
        params: list[Any] = []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if min_score is not None:
            where.append("score >= ?")
            params.append(min_score)
        if since_days is not None:
            where.append("first_seen_at >= ?")
            since = datetime.now(tz=UTC) - timedelta(days=since_days)
            params.append(since.isoformat(timespec="seconds"))
        if only_unnotified:
            where.append("notified_at IS NULL")
        if include_suspicious is False:
            where.append("suspicious = 0")
        elif include_suspicious:
            where.append("suspicious = 1")
        sql = "SELECT rowid AS id_row, * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY score DESC, first_seen_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur = await self.db.execute(sql, params)
        return list(await cur.fetchall())

    async def count_jobs(self, **kwargs: Any) -> int:
        kwargs.pop("limit", None)
        kwargs.pop("offset", None)
        kwargs.pop("only_unnotified", None)
        rows = await self.list_jobs(**kwargs, limit=10_000)
        return len(rows)

    async def followup_due(self, days: int = 6) -> list[sqlite3.Row]:
        cutoff = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        cur = await self.db.execute(
            "SELECT rowid AS id_row, * FROM jobs WHERE status='applied' AND followup_at IS NOT NULL"
            " AND followup_at <= ? ORDER BY followup_at",
            (cutoff,),
        )
        return list(await cur.fetchall())

    async def funnel_counts(self, since_days: int | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM jobs"
        params: list[Any] = []
        if since_days is not None:
            sql += " WHERE first_seen_at >= ?"
            params.append(
                (datetime.now(tz=UTC) - timedelta(days=since_days)).isoformat(timespec="seconds")
            )
        sql += " GROUP BY status"
        cur = await self.db.execute(sql, params)
        return {row["status"]: row["n"] for row in await cur.fetchall()}

    async def top_sources(self, since_days: int = 30) -> list[tuple[str, int, float]]:
        cutoff = (datetime.now(tz=UTC) - timedelta(days=since_days)).isoformat(timespec="seconds")
        cur = await self.db.execute(
            "SELECT source, COUNT(*) AS n, AVG(score) AS avg_score FROM jobs"
            " WHERE first_seen_at >= ? GROUP BY source ORDER BY n DESC LIMIT 10",
            (cutoff,),
        )
        return [(r["source"], r["n"], r["avg_score"] or 0.0) for r in await cur.fetchall()]

    # ---------------------------------------------------------------- events

    async def log_event(self, job_id: str | None, type_: str, payload: str = "") -> None:
        await self.db.execute(
            "INSERT INTO events(job_id, ts, type, payload) VALUES(?,?,?,?)",
            (job_id, now_iso(), type_, payload),
        )
        await self.db.commit()

    async def count_events(self, type_: str, since_days: int) -> int:
        cutoff = (datetime.now(tz=UTC) - timedelta(days=since_days)).isoformat(timespec="seconds")
        cur = await self.db.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type=? AND ts >= ?", (type_, cutoff)
        )
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def company_reject_tally(self, since_days: int = 30) -> dict[str, int]:
        """Companies the owner rejected — feeds the scoring feedback loop."""
        cutoff = (datetime.now(tz=UTC) - timedelta(days=since_days)).isoformat(timespec="seconds")
        cur = await self.db.execute(
            "SELECT company, COUNT(*) AS n FROM jobs WHERE status='rejected'"
            " AND updated_at >= ? GROUP BY company HAVING n >= 1 ORDER BY n DESC LIMIT 50",
            (cutoff,),
        )
        return {r["company"]: r["n"] for r in await cur.fetchall()}

    async def reject_reason_tally(self, since_days: int = 30) -> dict[str, int]:
        cutoff = (datetime.now(tz=UTC) - timedelta(days=since_days)).isoformat(timespec="seconds")
        cur = await self.db.execute(
            "SELECT reject_reason, COUNT(*) AS n FROM jobs WHERE status='rejected'"
            " AND reject_reason <> '' AND updated_at >= ? GROUP BY reject_reason"
            " ORDER BY n DESC",
            (cutoff,),
        )
        return {r["reject_reason"]: r["n"] for r in await cur.fetchall()}

    # --------------------------------------------------------- source health

    async def record_source_ok(self, name: str, jobs_found: int) -> None:
        await self.db.execute(
            """INSERT INTO source_health(name, last_ok_at, last_error_at, last_error,
                   consecutive_errors, jobs_found, runs)
               VALUES(?,?,NULL,'',0,?,1)
               ON CONFLICT(name) DO UPDATE SET last_ok_at=excluded.last_ok_at,
                   last_error='', consecutive_errors=0,
                   jobs_found=excluded.jobs_found, runs=runs+1""",
            (name, now_iso(), jobs_found),
        )
        await self.db.commit()

    async def record_source_error(self, name: str, error: str) -> None:
        cur = await self.db.execute(
            "SELECT consecutive_errors FROM source_health WHERE name=?", (name,)
        )
        row = await cur.fetchone()
        errors = (row["consecutive_errors"] if row else 0) + 1
        await self.db.execute(
            """INSERT INTO source_health(name, last_ok_at, last_error_at, last_error,
                   consecutive_errors, jobs_found, runs)
               VALUES(?,NULL,?,?,?,0,1)
               ON CONFLICT(name) DO UPDATE SET last_error_at=excluded.last_error_at,
                   last_error=excluded.last_error, consecutive_errors=excluded.consecutive_errors,
                   runs=runs+1""",
            (name, now_iso(), error[:200], errors),
        )
        await self.db.commit()

    async def source_health_all(self) -> list[SourceHealth]:
        cur = await self.db.execute("SELECT * FROM source_health ORDER BY name")
        return [
            SourceHealth(
                name=r["name"],
                last_ok_at=r["last_ok_at"],
                last_error_at=r["last_error_at"],
                last_error=r["last_error"],
                consecutive_errors=r["consecutive_errors"],
                jobs_found=r["jobs_found"],
                runs=r["runs"],
            )
            for r in await cur.fetchall()
        ]

    # -------------------------------------------------------------------- kv

    async def kv_get(self, key: str, default: str = "") -> str:
        cur = await self.db.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def kv_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.db.commit()

    # ------------------------------------------------------------- llm usage

    async def llm_spend_today(self) -> float:
        day = datetime.now(tz=UTC).date().isoformat()
        cur = await self.db.execute("SELECT cost_usd FROM llm_usage WHERE day=?", (day,))
        row = await cur.fetchone()
        return float(row["cost_usd"]) if row else 0.0

    async def llm_add_usage(self, calls: int, in_tok: int, out_tok: int, cost: float) -> None:
        day = datetime.now(tz=UTC).date().isoformat()
        await self.db.execute(
            """INSERT INTO llm_usage(day, calls, in_tokens, out_tokens, cost_usd)
               VALUES(?,?,?,?,?)
               ON CONFLICT(day) DO UPDATE SET calls=calls+excluded.calls,
                   in_tokens=in_tokens+excluded.in_tokens,
                   out_tokens=out_tokens+excluded.out_tokens,
                   cost_usd=cost_usd+excluded.cost_usd""",
            (day, calls, in_tok, out_tok, cost),
        )
        await self.db.commit()

    # ---------------------------------------------------------------- backup

    def backup(self, backup_dir: str | Path, keep: int = 7) -> Path | None:
        """Daily copy of the SQLite file with rotation (oldest deleted)."""
        if not self.path.exists():
            return None
        target = Path(backup_dir)
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        dest = target / f"jobhunter-{stamp}.db"
        shutil.copy2(self.path, dest)
        backups = sorted(target.glob("jobhunter-*.db"))
        for old in backups[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        log.info("backup written %s (keeping %d)", dest.name, keep)
        return dest
