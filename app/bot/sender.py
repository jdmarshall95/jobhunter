"""Outgoing message queue.

Telegram tolerates roughly 1 msg/s per chat and 20/min globally, so the bot
never sends inline: everything goes through a single worker that paces the
stream, splits long texts and survives ``RetryAfter``/``FloodWait``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.bot.render import split_message
from app.logging_setup import get_logger

log = get_logger(__name__)

#: how long to sleep between two messages of the same queue
DEFAULT_INTERVAL = 1.05
_MAX_FLOOD_WAIT = 60.0


@dataclass(slots=True)
class OutboxItem:
    chat_id: int
    text: str
    reply_markup: Any | None = None
    disable_preview: bool = True
    chunks: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.chunks = split_message(self.text)


class Sender:
    """Serialises outgoing messages; ``api`` is anything with ``send_message``."""

    def __init__(
        self,
        api: Any,
        *,
        interval: float = DEFAULT_INTERVAL,
        error_handler: Callable[[Exception], float | None] | None = None,
    ) -> None:
        self.api = api
        self.interval = interval
        self._queue: asyncio.Queue[OutboxItem | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._error_handler = error_handler
        self.sent = 0
        self.dropped = 0

    # ------------------------------------------------------------------ api
    def send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Any | None = None,
        disable_preview: bool = True,
    ) -> None:
        """Queue a message (fire and forget)."""
        self._queue.put_nowait(
            OutboxItem(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                disable_preview=disable_preview,
            )
        )

    async def send_now(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Any | None = None,
        disable_preview: bool = True,
    ) -> None:
        await self._dispatch(
            OutboxItem(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                disable_preview=disable_preview,
            )
        )

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker(), name="tg-sender")

    async def stop(self) -> None:
        """Flush what is queued, then stop the worker."""
        await self._queue.join()
        if self._task is not None:
            self._queue.put_nowait(None)
            await asyncio.wait([self._task], timeout=5)
            self._task = None

    async def drain(self) -> None:
        await self._queue.join()

    # ---------------------------------------------------------------- worker
    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await self._dispatch(item)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                log.error("sender failed: %s", exc)
                self.dropped += 1
            finally:
                self._queue.task_done()
            await asyncio.sleep(self.interval)

    async def _dispatch(self, item: OutboxItem) -> None:
        for index, chunk in enumerate(item.chunks):
            markup = item.reply_markup if index == len(item.chunks) - 1 else None
            await self._send_one(item, chunk, markup)
            self.sent += 1

    async def _send_one(self, item: OutboxItem, text: str, markup: Any | None) -> None:
        attempts = 0
        while True:
            try:
                await self.api.send_message(
                    chat_id=item.chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=item.disable_preview,
                    reply_markup=markup,
                )
                return
            except Exception as exc:  # noqa: BLE001 - RetryAfter/FloodWait handled here
                wait = self._flood_seconds(exc)
                attempts += 1
                if wait is None or attempts > 3 or wait > _MAX_FLOOD_WAIT:
                    raise
                log.warning("telegram flood-wait %.1fs (attempt %d)", wait, attempts)
                await asyncio.sleep(wait + 0.5)

    @staticmethod
    def _flood_seconds(exc: Exception) -> float | None:
        """Return the retry delay for RetryAfter/FloodWait, else ``None``."""
        for attr in ("retry_after", "e", "timeout"):
            value = getattr(exc, attr, None)
            if isinstance(value, (int, float)) and 0 < value <= _MAX_FLOOD_WAIT:
                return float(value)
        name = type(exc).__name__
        if name in ("RetryAfter", "FloodWait", "TelegramNetworkError", "TelegramServerError"):
            return 3.0
        return None


__all__ = ["Sender", "OutboxItem", "DEFAULT_INTERVAL"]
