"""Entry point.

Usage
-----
    python -m app.main --once          # one pipeline run, then exit
    python -m app.main --dry-run       # run everything but do not write to SQLite
    python -m app.main                 # bot + scheduler (what Docker runs)

Responsibilities: load config, connect storage, register secrets with the log
masker, expose ``/healthz``, start the outgoing queue + scheduler + long polling,
and shut everything down gracefully on SIGINT/SIGTERM.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from dataclasses import dataclass, field
from typing import Any

from app.bot.handlers import BotCtx, build_router
from app.bot.sender import Sender
from app.config import Settings, get_settings, load_profile, load_sources
from app.logging_setup import get_logger, register_secret, setup_logging
from app.pipeline.runner import Pipeline, RunSummary
from app.scheduler import Scheduler
from app.storage import Database

log = get_logger(__name__)


# ------------------------------------------------------------------ healthz
class HealthzServer:
    """Tiny TCP server answering one HTTP request with 200/500 (no deps)."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.healthy = True
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> HealthzServer:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("healthz listening on %s:%d/healthz", self.host, self.port)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=5)
        except (TimeoutError, ConnectionError):  # pragma: no cover - defensive
            request = b""
        parts = request.split(b" ")
        path = request.decode("latin-1").split(" ")[1] if len(parts) > 1 else "/healthz"
        status, body = (200, "ok") if self.healthy else (503, "degraded")
        writer.write(
            f"HTTP/1.1 {status} {body}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode()
        )
        with contextlib.suppress(Exception):
            await writer.drain()
            writer.close()
        if path not in ("/healthz", "/health"):
            log.debug("healthz request %s", path.strip())


# ------------------------------------------------------------------ app shell
@dataclass
class App:
    """Composition root: everything wired, nothing started."""

    settings: Settings
    db: Database
    pipeline: Pipeline
    sender: Sender
    ctx: BotCtx
    scheduler: Scheduler
    bot: Any | None = None
    tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    async def start_bot(self) -> None:
        from aiogram import Bot, Dispatcher

        bot = Bot(token=self.settings.telegram_bot_token)
        dp = Dispatcher()
        dp.include_router(build_router(self.ctx))
        self.bot = bot
        self.ctx.api = bot
        # the sender queue needs the same transport, otherwise cards go nowhere
        self.sender.api = bot
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception as exc:  # noqa: BLE001 - network hiccup must not abort
            log.warning("delete_webhook failed: %s", exc)
        # start_polling lives on Dispatcher (aiogram 3), not on Bot
        polling = dp.start_polling(
            bot, handle_signals=False, close_bot_session=False
        )
        self.tasks.append(asyncio.create_task(polling, name="polling"))
        log.info("bot polling started (owner whitelist: %s)", sorted(self.ctx.owners))

    async def stop(self) -> None:
        self.scheduler.stop()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self.sender.stop()
        if self.bot is not None:
            with contextlib.suppress(Exception):
                await self.bot.close()
        await self.pipeline.close()
        await self.db.close()


def build_app(settings: Settings, *, llm_enabled: bool = True) -> App:
    """Create the object graph (DB stays closed until :meth:`App.prepare`)."""
    profile = load_profile()
    db = Database(settings.db_path)
    pipeline = Pipeline(settings, profile, db)
    sender = Sender(api=None)
    ctx = BotCtx(
        settings=settings,
        profile=profile,
        db=db,
        pipeline=pipeline,
        sender=sender,
        owners=settings.owner_chat_ids(),
    )
    scheduler = Scheduler(ctx)
    if not llm_enabled:
        pipeline.llm.disable()
    log.info(
        "wired: sources=%d enabled=%d llm=%s owners=%s",
        len(load_sources().sources),
        sum(1 for s in load_sources().sources if s.enabled),
        pipeline.llm.enabled,
        sorted(ctx.owners),
    )
    return App(
        settings=settings, db=db, pipeline=pipeline, sender=sender, ctx=ctx, scheduler=scheduler
    )


async def prepare(app: App) -> None:
    await app.db.connect()


def _on_run(summary: RunSummary) -> None:
    log.info("scheduled run: %s", summary.as_text())


async def run_forever(settings: Settings, *, with_bot: bool = True) -> int:
    app = build_app(settings)
    app.scheduler.on_run = _on_run
    await prepare(app)

    health = HealthzServer("0.0.0.0", settings.healthz_port)
    async with health:
        try:
            app.sender.start()
            if with_bot and settings.telegram_available:
                await app.start_bot()
            elif with_bot:
                log.warning(
                    "Telegram disabled: set TELEGRAM_ENABLED/TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID"
                )
            app.scheduler.start()

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)
            await stop.wait()
        finally:
            log.info("shutting down")
            await app.stop()
    return 0


async def run_once(settings: Settings, *, dry_run: bool) -> int:
    app = build_app(settings)
    await prepare(app)
    app.sender.start()
    try:
        summary = await app.pipeline.run(dry_run=dry_run)
        print(f"{'[dry-run] ' if dry_run else ''}{summary.as_text()}")
        for result in summary.sources:
            mark = "✅" if result.ok else "❌"
            detail = f"{result.jobs} вакансий" if result.ok else result.error
            print(f"  {mark} {result.name}: {detail} ({result.seconds:.1f}s)")
        if not dry_run:
            from app.bot.handlers import notify_new

            await notify_new(app.ctx, summary.new_ids)
            await app.sender.drain()
    finally:
        await app.stop()
    return 0 if summary.ok_sources else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jobhunter", description="Vacancy hunter + Telegram bot")
    parser.add_argument("--once", action="store_true", help="run the pipeline once and exit")
    parser.add_argument("--dry-run", action="store_true", help="do not write to SQLite")
    parser.add_argument("--no-bot", action="store_true", help="scheduler only, no Telegram polling")
    parser.add_argument("--no-llm", action="store_true", help="ignore the LLM key for this run")
    parser.add_argument("--print-config", action="store_true", help="dump resolved config and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    register_secret(settings.telegram_bot_token)
    register_secret(settings.gemini_api_key)

    if args.print_config:
        print(f"tz={settings.timezone} db={settings.db_path} digest={settings.digest_time}")
        print(f"llm={'on' if settings.llm_available else 'off'} tg={settings.telegram_available}")
        print(f"owners={sorted(settings.owner_chat_ids())}")
        for src in load_sources().sources:
            print(f"  {'on ' if src.enabled else 'off'} {src.label} [{src.kind}]")
        return 0

    if args.once or args.dry_run:
        return asyncio.run(run_once(settings, dry_run=args.dry_run))
    try:
        return asyncio.run(run_forever(settings, with_bot=not args.no_bot))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
