"""Pipeline orchestration: fetch -> normalize -> dedup -> filters -> score -> persist.

Design notes
------------
* Error isolation: a source that raises (or returns nothing) is recorded in
  ``source_health`` and never aborts the run (ТЗ §5).
* The LLM step is strictly additive — when there is no API key or the daily
  budget is spent, the rule-based score is what the user sees (ТЗ §6).
* ``dry_run`` performs every step except writing to the database, so ``--dry-run``
  is a safe smoke test on a production database.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Profile, Settings, SourceConfig
from app.http import HttpFetcher
from app.llm import GeminiClient, LLMService
from app.logging_setup import get_logger
from app.models import Job, RawJob
from app.pipeline.dedup import dedupe
from app.pipeline.filters import apply_hard_filters
from app.pipeline.normalize import normalize
from app.scoring import FxCache, score_job
from app.sources import SourcePlugin, TelegramSource, build_sources
from app.storage import Database

log = get_logger(__name__)

#: kv key holding the bot-managed company blocklist (``/block``, ``/unblock``).
KV_BLOCKLIST = "blocklist"
#: kv key holding the bot-managed Telegram channels (``/addchannel``, ``/rmchannel``).
KV_CHANNELS = "channels"


@dataclass(slots=True)
class SourceResult:
    """Per-source outcome of one run."""

    name: str
    ok: bool
    jobs: int = 0
    error: str = ""
    seconds: float = 0.0


@dataclass(slots=True)
class RunSummary:
    """Everything one pipeline run reports back to the bot/scheduler."""

    sources: list[SourceResult] = field(default_factory=list)
    raw_total: int = 0
    duplicates: int = 0
    filtered: int = 0
    accepted: int = 0
    new_ids: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    llm_calls: int = 0
    seconds: float = 0.0

    @property
    def ok_sources(self) -> int:
        return sum(1 for s in self.sources if s.ok)

    def as_text(self) -> str:
        parts = [
            f"источников {self.ok_sources}/{len(self.sources)}",
            f"получено {self.raw_total}",
            f"дублей {self.duplicates}",
            f"отсеяно {self.filtered}",
            f"принято {self.accepted}",
            f"новых {len(self.new_ids)}",
        ]
        if self.llm_calls:
            parts.append(f"LLM {self.llm_calls}")
        parts.append(f"{self.seconds:.1f}s")
        return ", ".join(parts)


class Pipeline:
    """Owns the HTTP client, the source plugins and the storage handle."""

    def __init__(
        self,
        settings: Settings,
        profile: Profile,
        db: Database,
        *,
        http: HttpFetcher | None = None,
        llm: LLMService | None = None,
        sources: list[SourcePlugin] | None = None,
    ) -> None:
        self.settings = settings
        self.profile = profile
        self.db = db
        self.http = http or HttpFetcher(
            user_agent=settings.user_agent,
            timeout=settings.http_timeout,
            max_retries=settings.http_max_retries,
            domain_min_interval=settings.domain_min_interval,
        )
        self.llm = llm or LLMService(
            settings,
            db,
            client=GeminiClient(settings) if settings.llm_available else None,
        )
        self.sources = (
            sources
            if sources is not None
            else build_sources(self._sources_config(), self.http)
        )
        self.fx = FxCache()

    @staticmethod
    def _sources_config() -> Any:
        from app.config import load_sources

        return load_sources()

    # ------------------------------------------------------------------ fetch
    async def _fetch_one(
        self, source: SourcePlugin, sem: asyncio.Semaphore
    ) -> tuple[SourceResult, list[RawJob]]:
        started = time.monotonic()
        async with sem:
            try:
                raw = await source.fetch()
            except Exception as exc:  # noqa: BLE001 - error isolation is the point
                seconds = time.monotonic() - started
                message = f"{type(exc).__name__}: {exc}"
                log.warning("source %s failed: %s", source.name, message)
                await self.db.record_source_error(source.name, message)
                return SourceResult(
                    name=source.name, ok=False, error=message, seconds=seconds
                ), []
        seconds = time.monotonic() - started
        log.info("source %s ok jobs=%d in %.1fs", source.name, len(raw), seconds)
        await self.db.record_source_ok(source.name, len(raw))
        return SourceResult(name=source.name, ok=True, jobs=len(raw), seconds=seconds), raw

    async def fetch_all(self) -> tuple[list[RawJob], list[SourceResult]]:
        """Fetch every source in parallel (bounded), keeping failures isolated."""
        await self._apply_channels()
        sem = asyncio.Semaphore(max(1, self.settings.fetch_concurrency))
        outcomes = await asyncio.gather(*(self._fetch_one(s, sem) for s in self.sources))
        raw: list[RawJob] = []
        results: list[SourceResult] = []
        for result, jobs in outcomes:
            results.append(result)
            raw.extend(jobs)
        return raw, results

    # ------------------------------------------------------------------- run
    async def run(self, *, dry_run: bool = False, use_llm: bool | None = None) -> RunSummary:
        started = time.monotonic()
        summary = RunSummary()

        raw, source_results = await self.fetch_all()
        summary.sources = source_results
        summary.raw_total = len(raw)

        jobs = [normalize(item) for item in raw]
        jobs = dedupe(jobs)
        summary.duplicates = max(0, len(raw) - len(jobs))

        stopwords = await self._stopwords()
        negative = await self.db.company_reject_tally()
        blocklist = await self._blocklist()

        # Persist every vacancy that survives hard filters — even below
        # ``min_score``. Otherwise the web UI / channel feeds look empty while
        # suitable Luna Park posts quietly disappear (score often 0–30 when the
        # title is not literally «DevOps»). ``min_score`` only gates digests,
        # Telegram alerts and LLM re-score.
        saved: list[Job] = []
        for job in jobs:
            outcome = apply_hard_filters(
                job, self.profile, extra_stopwords=stopwords, blocklist=blocklist
            )
            if not outcome.passed:
                summary.filtered += 1
                log.debug(
                    "filtered %s | %s: %s", job.company, job.title, "; ".join(outcome.reasons)
                )
                continue
            # ``score_job`` rebuilds ``job.flags`` from scratch, so the filter notes
            # are merged back in afterwards: a geo note must reach the card (ТЗ §6).
            score_job(job, self.profile, self.fx, negative_signals=negative)
            for note in outcome.flags:
                if note not in job.flags:
                    job.flags.append(note)
            saved.append(job)

        qualified = [j for j in saved if j.score >= self.profile.min_score]

        want_llm = self.llm.enabled if use_llm is None else use_llm
        if want_llm:
            for job in qualified:
                result = await self.llm.rescore(job, self.profile)
                if result is not None:
                    summary.llm_calls += 1

        for job in saved:
            qualifies = job.score >= self.profile.min_score
            if dry_run:
                if qualifies:
                    summary.new_ids.append(job.id)
                continue
            _, is_new = await self.db.upsert_job(job)
            if is_new and qualifies:
                summary.new_ids.append(job.id)
                await self.db.log_event(job.id, "new", json.dumps({"sources": job.sources}))
            if job.score >= self.profile.alert_score:
                summary.alerts.append(job.id)

        summary.accepted = len(saved)
        summary.seconds = time.monotonic() - started
        log.info("run finished: %s", summary.as_text())
        return summary

    # ------------------------------------------------------- bot-facing state
    async def _apply_channels(self) -> None:
        """Bot-managed channels (``/addchannel``) extend the configured TG list."""
        plugins = [s for s in self.sources if isinstance(s, TelegramSource)]
        if not plugins:
            return
        try:
            names = json.loads(await self.db.kv_get(KV_CHANNELS, "[]") or "[]")
        except json.JSONDecodeError:
            log.warning("kv %s is not valid JSON — using config only", KV_CHANNELS)
            return
        for plugin in plugins:
            plugin.add_channels([n for n in names if isinstance(n, str)])

    async def _blocklist(self) -> list[str]:
        raw = await self.db.kv_get(KV_BLOCKLIST, "[]")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    async def _stopwords(self) -> list[str]:
        """Extra stop-words learned from the owner's reject reasons (ТЗ §7)."""
        raw = await self.db.kv_get("stopwords", "[]")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - defensive
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    async def add_block_company(self, company: str) -> list[str]:
        blocklist = await self._blocklist()
        if company and company not in blocklist:
            blocklist.append(company)
            await self.db.kv_set(KV_BLOCKLIST, json.dumps(blocklist, ensure_ascii=False))
        return blocklist

    async def remove_block_company(self, company: str) -> list[str]:
        blocklist = [c for c in await self._blocklist() if c != company]
        await self.db.kv_set(KV_BLOCKLIST, json.dumps(blocklist, ensure_ascii=False))
        return blocklist

    async def close(self) -> None:
        await self.http.aclose()
        client = getattr(self.llm, "_client", None)
        if client is not None and hasattr(client, "aclose"):
            await client.aclose()


def describe_source(cfg: SourceConfig) -> str:  # pragma: no cover - reporting helper
    return f"{cfg.label} [{cfg.kind}]"


__all__ = ["Pipeline", "RunSummary", "SourceResult", "KV_BLOCKLIST", "describe_source"]
