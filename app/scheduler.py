"""APScheduler wiring (ТЗ §3, §7).

Jobs
----
* ``pipeline``      — every ``FETCH_INTERVAL_MINUTES``; pushes fresh cards.
* ``digest``        — cron at ``DIGEST_TIME`` (09:00 by default).
* ``weekly``        — Monday morning weekly report.
* ``followups``     — reminders for vacancies sitting in ``applied`` for N days.
* ``backup``        — daily SQLite copy with rotation.

The scheduler never raises into the caller: every job is wrapped so that one
bad run cannot kill the loop (ТЗ §8 «устойчивость»).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.bot.handlers import BotCtx, conversions, notify_new, send_card
from app.bot.keyboards import followup_keyboard
from app.bot.render import esc, job_view, render_digest, render_stats
from app.config import Settings
from app.logging_setup import get_logger
from app.pipeline.runner import RunSummary
from app.timeutil import now_in

log = get_logger(__name__)

#: how many vacancies the weekly report lists as «best of the week»
WEEKLY_TOP = 5


def _hhmm(value: str) -> tuple[int, int]:
    """Parse ``"09:00"`` defensively; anything odd falls back to 09:00."""
    try:
        hour_raw, minute_raw = value.strip().split(":")
        hour, minute = int(hour_raw), int(minute_raw)
    except (ValueError, AttributeError):
        return 9, 0
    return hour % 24, max(0, min(59, minute))


class Scheduler:
    """Thin facade over :class:`AsyncIOScheduler` bound to a :class:`BotCtx`."""

    def __init__(self, ctx: BotCtx, *, on_run: Callable[[RunSummary], None] | None = None) -> None:
        self.ctx = ctx
        self.settings: Settings = ctx.settings
        self.on_run = on_run
        self._run_lock_busy = False
        self.scheduler = AsyncIOScheduler(timezone=self.settings.timezone)

    # --------------------------------------------------------------- lifecycle
    def start(self) -> None:
        s = self.settings
        hour, minute = _hhmm(s.digest_time)

        self.scheduler.add_job(
            self.safe_run,
            IntervalTrigger(minutes=max(5, s.fetch_interval_minutes)),
            id="pipeline",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(tz=self.scheduler.timezone),
        )
        self.scheduler.add_job(
            self.send_digest,
            CronTrigger(hour=hour, minute=minute, timezone=s.timezone),
            id="digest",
            coalesce=True,
        )
        self.scheduler.add_job(
            self.send_weekly,
            CronTrigger(
                day_of_week=s.weekly_report_weekday,
                hour=hour,
                minute=minute + 1,
                timezone=s.timezone,
            ),
            id="weekly",
            coalesce=True,
        )
        self.scheduler.add_job(
            self.check_followups,
            CronTrigger(hour=hour, minute=min(59, minute + 10), timezone=s.timezone),
            id="followups",
            coalesce=True,
        )
        self.scheduler.add_job(
            self.daily_backup,
            CronTrigger(hour=3, minute=30, timezone=s.timezone),
            id="backup",
            coalesce=True,
        )
        self.scheduler.start()
        log.info(
            "scheduler started tz=%s digest=%s interval=%sm",
            s.timezone,
            s.digest_time,
            s.fetch_interval_minutes,
        )

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    # -------------------------------------------------------------------- jobs
    async def safe_run(self) -> RunSummary | None:
        """Pipeline run guarded against overlap and exceptions."""
        if self._run_lock_busy:
            log.info("pipeline run skipped: previous still running")
            return None
        self._run_lock_busy = True
        try:
            summary = await self.ctx.pipeline.run()
            if self.on_run is not None:
                self.on_run(summary)
            await notify_new(self.ctx, summary.new_ids)
            await self._send_alerts(summary)
            return summary
        except Exception as exc:  # noqa: BLE001 - the scheduler must survive
            log.error("pipeline job failed: %s", exc, exc_info=True)
            await self._notify_error(exc)
            return None
        finally:
            self._run_lock_busy = False

    async def send_digest(self) -> None:
        """Top-10 of the last day (ТЗ §7 «дайджест»)."""
        if await self.ctx.paused():
            log.info("digest skipped: paused")
            return
        rows = await self.ctx.db.list_jobs(since_days=1, limit=10, include_suspicious=False)
        new_count = await self.ctx.db.count_jobs(since_days=1)
        text = render_digest(
            [job_view(r) for r in rows],
            # the digest fires at DIGEST_TIME local, so its date must be local too
            date_text=now_in(self.settings.timezone).strftime("%d.%m"),
            new_count=new_count,
        )
        self.ctx.sender.send(self._chat(), text)

    async def send_weekly(self) -> None:
        """Monday report: funnel + conversions + best sources of the week."""
        funnel = await self.ctx.db.funnel_counts(since_days=7)
        sources = await self.ctx.db.top_sources(since_days=7)
        text = render_stats(
            funnel=funnel,
            conversions=conversions(funnel),
            best_sources=sources,
            window_days=7,
        )
        top = await self.ctx.db.list_jobs(since_days=7, limit=WEEKLY_TOP, include_suspicious=False)
        if top:
            lines = ["", "лучшее за неделю:"]
            lines += [
                (
                    f"· <b>{r['score']:.0f}</b>"
                    f" <a href=\"{esc(r['url'])}\">{esc(r['title'])}</a> — {esc(r['company'])}"
                )
                for r in top
            ]
            text += "\n".join(lines)
        self.ctx.sender.send(self._chat(), text)

    async def check_followups(self) -> None:
        """Remind about applications that are silent for ``followup_days``."""
        rows = await self.ctx.db.followup_due(days=self.settings.followup_days)
        if not rows:
            return
        chat = self._chat()
        for row in rows[:5]:
            silence = f"тишина {self.settings.followup_days}+ дней."
            text = (
                f"⏰ Фоллоу-ап: <b>{esc(row['title'])}</b> — {esc(row['company'])}\n"
                f"Отклик от {str(row['updated_at'])[:10]}, {silence}"
            )
            self.ctx.sender.send(chat, text, reply_markup=followup_keyboard(str(row["id"])))
        log.info("follow-up reminders sent: %d", min(len(rows), 5))

    async def daily_backup(self) -> None:
        path = self.ctx.db.backup(self.settings.backup_dir, self.settings.backup_keep)
        if path is not None:
            log.info("sqlite backup: %s", path)

    # ----------------------------------------------------------------- helpers
    async def _send_alerts(self, summary: RunSummary) -> None:
        """Instant alerts for ``score >= ALERT_SCORE`` (max N per hour)."""
        if not summary.alerts or await self.ctx.paused():
            return
        budget = max(1, self.settings.alerts_per_hour)
        sent = 0
        for job_id in summary.alerts:
            if sent >= budget:
                log.info("alert budget reached, %d alerts deferred", len(summary.alerts) - sent)
                break
            row = await self.ctx.db.get_job(job_id)
            if row is None:  # pragma: no cover - defensive
                continue
            await send_card(self.ctx, row)
            sent += 1
        if sent:
            await self.ctx.db.log_event(None, "alert", str(sent))

    async def _notify_error(self, exc: Exception) -> None:
        if await self.ctx.paused():
            return
        self.ctx.sender.send(self._chat(), f"⚠️ сбор упал: <i>{esc(str(exc)[:200])}</i>")

    def _chat(self) -> int:
        ids = sorted(self.ctx.owners)
        return ids[0] if ids else 0


def build_scheduler(ctx: BotCtx) -> Scheduler:
    """Factory used by ``main`` and by tests."""
    return Scheduler(ctx)


__all__ = ["Scheduler", "WEEKLY_TOP", "build_scheduler"]
