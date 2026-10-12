"""Telegram-каналы (ТЗ §5.6) через публичный веб-превью ``t.me/s/<channel>``.

Telethon подключается только когда есть ``TG_API_ID``/``TG_API_HASH`` (в ``.env``
их может не быть). Поэтому базовый путь — обычный HTTP-парсинг публичной
страницы канала: он не требует сессии и не упирается в лимиты «новый аккаунт».

Разметка превью (проверено на ``devops_vacancies``):

    <div class="tgme_widget_message" data-post="devops_vacancies/42">
      <a class="tgme_widget_message_owner_name"><span>DevOps вакансии | работа</span></a>
      <div class="tgme_widget_message_text js-message_text">…</div>
      <time datetime="2025-08-30T03:08:05+00:00">…</time>

Имена каналов берутся из ``config/sources.yaml`` (``queries``) и из
бот-управляемого списка (``/addchannel``), который пайплайн подкладывает через
:meth:`TelegramSource.add_channels`.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from app.models import RawJob
from app.sources.base import SourcePlugin

PREVIEW_URL = "https://t.me/s/{channel}"

#: Каналы по умолчанию: проверены, отдают непустое превью без авторизации.
DEFAULT_CHANNELS = ("devops_vacancies", "habr_career", "getjob")

#: «DevOps-инженер» / «Системный инженер/DevOps Engineer (KSS)» — первая строка
_WS_RE = re.compile(r"[ \t\u00a0]+")
_TITLE_MAX = 120


def _clean_lines(text: str) -> list[str]:
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _title_of(lines: list[str]) -> str:
    """First meaningful line: emoji-only and bullet-only lines are skipped."""
    for line in lines:
        if len(line) < 4:
            continue
        if line in {"ссылка", "Подробнее", "read more"}:
            continue
        return line[:_TITLE_MAX]
    return (lines[0] if lines else "")[:_TITLE_MAX]


def parse_telegram(
    html: str,
    *,
    channel: str,
    source: str,
    queries: list[str] | None = None,
    limit: int = 20,
) -> list[RawJob]:
    soup = BeautifulSoup(html or "", "html.parser")
    needles = [q.casefold() for q in (queries or []) if q]
    jobs: list[RawJob] = []
    for node in soup.select("div.tgme_widget_message"):
        post = str(node.get("data-post") or "").strip()
        text_node = node.select_one("div.tgme_widget_message_text")
        if not text_node:
            continue
        text = text_node.get_text("\n", strip=True)
        lines = _clean_lines(text)
        if not lines:
            continue
        if needles and not any(n in text.casefold() for n in needles):
            continue
        time_node = node.select_one("time")
        author_node = node.select_one("a.tgme_widget_message_owner_name")
        company = (
            author_node.get_text(" ", strip=True) if author_node else ""
        ) or f"@{channel}"
        jobs.append(
            RawJob(
                source=source,
                company=company,
                title=_title_of(lines),
                url=f"https://t.me/{post}" if post else PREVIEW_URL.format(channel=channel),
                location="",
                # None: канал пишет «удалёнка» не всегда — решает detect_remote.
                remote=None,
                description=text,
                posted_at=(time_node.get("datetime") if time_node else None),  # type: ignore[arg-type]
                raw={"post": post, "channel": channel},
            )
        )
        if len(jobs) >= limit:
            break
    return jobs


class TelegramSource(SourcePlugin):
    """One plugin instance scrapes a list of public channels."""

    kind = "telegram"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._extra: list[str] = []

    def add_channels(self, names: list[str]) -> None:
        """Merge bot-managed channels (``/addchannel``) into the config list."""
        for name in names:
            clean = (name or "").strip().lstrip("@")
            if clean and clean not in self._extra:
                self._extra.append(clean)

    @property
    def channels(self) -> list[str]:
        configured = [c.strip().lstrip("@") for c in (self.cfg.queries or []) if c.strip()]
        merged = configured + [c for c in self._extra if c not in configured]
        return merged or list(DEFAULT_CHANNELS)

    async def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        errors: list[str] = []
        for channel in self.channels:
            body, status = await self.http.get_text(PREVIEW_URL.format(channel=channel))
            if body is None:
                errors.append(f"@{channel} (http {status})")
                continue
            jobs.extend(parse_telegram(body, channel=channel, source=self.name))
        if not jobs:
            detail = ", ".join(errors) if errors else "no messages"
            raise self._fail(f"telegram feed empty: {detail}")
        return jobs


__all__ = ["TelegramSource", "parse_telegram", "PREVIEW_URL", "DEFAULT_CHANNELS"]
