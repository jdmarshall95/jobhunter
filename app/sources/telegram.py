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

Каналы вроде ``hrlunapark`` (Luna Park) пишут посты в формате
``tl;dr: role, $XXk–YYk, remote`` + компания в первой строке тела.

Превью ``t.me/s`` отдаёт ~20 последних постов; более старые добираются
пагинацией ``?before=<msg_id>`` (без Telethon).
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from app.models import RawJob
from app.sources.base import SourcePlugin

PREVIEW_URL = "https://t.me/s/{channel}"
PREVIEW_BEFORE_URL = "https://t.me/s/{channel}?before={before}"

#: Каналы по умолчанию: проверены, отдают непустое превью без авторизации.
DEFAULT_CHANNELS = ("devops_vacancies", "habr_career", "getjob", "hrlunapark")

#: How many preview pages to walk backwards (≈20 posts each).
DEFAULT_PAGES = 5
#: Cap per page — the widget rarely shows more than ~20 anyway.
PAGE_LIMIT = 25

#: «DevOps-инженер» / «Системный инженер/DevOps Engineer (KSS)» — первая строка
_WS_RE = re.compile(r"[ \t\u00a0]+")
_TITLE_MAX = 120
_TLDR_RE = re.compile(r"^tl;?\s*dr\s*:\s*(.+)$", re.IGNORECASE)
#: «Reelful — приложение…», «Recraft — …», «Acme - hiring…»
_COMPANY_LEAD_RE = re.compile(
    r"^([A-Za-zА-Яа-яЁё0-9][\w.&'’-]{0,48}?)\s*[—–\-]\s+\S",
    re.UNICODE,
)
_POST_ID_RE = re.compile(r"/(\d+)$")


def _clean_lines(text: str) -> list[str]:
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _title_of(lines: list[str]) -> str:
    """Prefer ``tl;dr: …`` payload; otherwise the first meaningful line."""
    for line in lines:
        m = _TLDR_RE.match(line)
        if m and m.group(1).strip():
            return m.group(1).strip()[:_TITLE_MAX]
    for line in lines:
        if len(line) < 4:
            continue
        if line in {"ссылка", "Подробнее", "read more"}:
            continue
        return line[:_TITLE_MAX]
    return (lines[0] if lines else "")[:_TITLE_MAX]


def _company_from_body(lines: list[str], *, channel: str, channel_title: str) -> str:
    """Pull the hiring company out of Luna Park-style posts when possible."""
    for line in lines:
        if _TLDR_RE.match(line):
            continue
        m = _COMPANY_LEAD_RE.match(line)
        if m:
            name = m.group(1).strip()
            # Skip meta openers that aren't company names.
            if name.casefold() not in {"это", "tl", "update", "как", "для", "ищем"}:
                return name
    # Aggregator title («Take Friends to Luna Park») is not the employer.
    if channel.casefold() == "hrlunapark":
        return f"@{channel}"
    return channel_title or f"@{channel}"


def _oldest_post_id(jobs: list[RawJob]) -> int | None:
    ids: list[int] = []
    for job in jobs:
        post = str((job.raw or {}).get("post") or "")
        m = _POST_ID_RE.search(post)
        if m:
            ids.append(int(m.group(1)))
    return min(ids) if ids else None


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
        channel_title = author_node.get_text(" ", strip=True) if author_node else ""
        company = _company_from_body(lines, channel=channel, channel_title=channel_title)
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
        self.pages = DEFAULT_PAGES

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

    async def _fetch_channel(self, channel: str) -> tuple[list[RawJob], str | None]:
        """Walk ``pages`` of the public preview, oldest-first via ``?before=``."""
        jobs: list[RawJob] = []
        seen: set[str] = set()
        url = PREVIEW_URL.format(channel=channel)
        last_status: int | None = None
        for _ in range(max(1, self.pages)):
            body, status = await self.http.get_text(url)
            last_status = status
            if body is None:
                if not jobs:
                    return [], f"@{channel} (http {status})"
                break
            batch = parse_telegram(
                body, channel=channel, source=self.name, limit=PAGE_LIMIT
            )
            fresh = 0
            for job in batch:
                post = str((job.raw or {}).get("post") or job.url)
                if post in seen:
                    continue
                seen.add(post)
                jobs.append(job)
                fresh += 1
            oldest = _oldest_post_id(batch)
            if fresh == 0 or oldest is None:
                break
            url = PREVIEW_BEFORE_URL.format(channel=channel, before=oldest)
        return jobs, None if jobs else f"@{channel} (http {last_status})"

    async def fetch(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        errors: list[str] = []
        for channel in self.channels:
            batch, error = await self._fetch_channel(channel)
            if error:
                errors.append(error)
            jobs.extend(batch)
        if not jobs:
            detail = ", ".join(errors) if errors else "no messages"
            raise self._fail(f"telegram feed empty: {detail}")
        return jobs


__all__ = [
    "TelegramSource",
    "parse_telegram",
    "PREVIEW_URL",
    "PREVIEW_BEFORE_URL",
    "DEFAULT_CHANNELS",
    "DEFAULT_PAGES",
]
