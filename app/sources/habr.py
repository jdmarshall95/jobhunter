"""Хабр Карьера RSS (проверено: 200, 50 объявлений, без авторизации).

``https://career.habr.com/vacancies/rss``

Публичный JSON-API (``/api/v1/vacancies``) отдаёт 401 без сессионной куки,
поэтому RSS — единственный endpoint без лишней зависимости. Формат объявления:

    title       Требуется «Системный администратор» (Ярославль)
    author      Raft Digital Solutions          <- компания
    description Компания «X» ищет … «роль». Ярославль (Россия).
                [От 100 000 ₽ до 200 000 ₽.] Полный рабочий день.
                [Можно удалённо.] Требуемые навыки: #middle, #Linux, …
    link        https://career.habr.com/vacancies/<id>

Две особенности, которые общий пайплайн сам не переваривает:

* зарплата по-русски — «от 300 000 ₽ до 500 000 ₽». Общий
  :func:`app.pipeline.normalize.parse_salary` такую форму не видит (его
  регулярки требуют символ валюты *перед* цифрами), поэтому числа вытаскиваются
  здесь и передаются явным диапазоном в RUB;
* про удалёнку написано «Можно удал**ё**нно», а ``REMOTE_TOKENS`` знает только
  ``удаленн`` — флаг выставляется явно.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import feedparser

from app.models import RawJob
from app.sources.base import SourcePlugin

FEED_URL = "https://career.habr.com/vacancies/rss"

#: «Требуется «Системный администратор» (Ярославль)» -> роль + хвост в скобках
_ROLE_RE = re.compile(r"«(?P<role>[^»]+)»\s*(?:\((?P<tail>[^)]*)\))?\s*$")
#: «Компания «Raft Digital Solutions» ищет…» — запасной источник компании
_COMPANY_RE = re.compile(r"Компания\s+«(?P<company>[^»]+)»", re.IGNORECASE)
#: Город следует сразу за фразой про вакансию: «…«роль». Ярославль (Россия).»
_LOCATION_RE = re.compile(r"»\.\s*(?P<city>[^.»]+?)\s*(?:\((?P<country>[^)]+)\))?\.(?:\s|$)")
#: Навыки: «Требуемые навыки: #middle, #Linux, …» (точка в конце предложения — не часть тега)
_SKILL_RE = re.compile(r"#([\w\u0400-\u04ff][\w\u0400-\u04ff+.-]*\w)")
#: «Можно удалённо» / «удаленка» / «remote» — с folding ё -> е
_REMOTE_RE = re.compile(r"удал[её]нн|remote|anywhere", re.IGNORECASE)

# Thousands are separated by a space or NBSP in the feed.
_MONEY = r"\d{1,3}(?:[\s\u00a0]\d{3})+|\d+"
_SALARY_RANGE_RE = re.compile(rf"от\s+({_MONEY})\s*₽\s*до\s+({_MONEY})\s*₽", re.IGNORECASE)
_SALARY_FROM_RE = re.compile(rf"от\s+({_MONEY})\s*₽", re.IGNORECASE)
_SALARY_TO_RE = re.compile(rf"до\s+({_MONEY})\s*₽", re.IGNORECASE)

#: Фразы, которые regex примет за город, если его в объявлении нет.
_NOT_A_LOCATION = frozenset(
    {
        "полный рабочий день",
        "гибкий график",
        "можно удалённо",
        "можно удаленно",
        "полная занятость",
        "частичная занятость",
        "проектная работа",
        "испытательный срок",
        "требуемые навыки",
    }
)


def _number(raw: str) -> float | None:
    """``"300 000"`` -> ``300000.0`` (spaces and NBSP are separators)."""
    digits = re.sub(r"[\s\u00a0,]", "", raw)
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def parse_salary_rub(text: str) -> tuple[float | None, float | None]:
    """Extract a Russian-style monthly range: «от X ₽ до Y ₽», «до Y ₽», «от X ₽»."""
    if not text:
        return None, None
    if m := _SALARY_RANGE_RE.search(text):
        return _number(m.group(1)), _number(m.group(2))
    lo = _SALARY_FROM_RE.search(text)
    hi = _SALARY_TO_RE.search(text)
    return (
        _number(lo.group(1)) if lo else None,
        _number(hi.group(1)) if hi else None,
    )


def _published(entry: dict[str, Any]) -> datetime | None:
    """RFC-822 -> aware UTC datetime (``parse_posted`` ISO-форматы его не берут)."""
    for key in ("published_parsed", "updated_parsed"):
        stamp = entry.get(key)
        if stamp:
            return datetime(*stamp[:6], tzinfo=UTC)  # type: ignore[misc]
    return None


def _clean_title(title: str) -> str:
    """Strip the «Требуется …» wrapper and the trailing ``(city, salary)`` parens."""
    m = _ROLE_RE.search(title)
    if m and (m.group("role") or "").strip():
        return m.group("role").strip()
    cleaned = re.sub(r"^(требуется|ищем|нужен)\s+", "", title.strip(), flags=re.IGNORECASE)
    return re.sub(r"\s*\([^()]*\)\s*$", "", cleaned).strip()


def _location(description: str, tail: str) -> str:
    """City (country) from the description, else the first segment of the parens."""
    m = _LOCATION_RE.search(description)
    if m:
        city = (m.group("city") or "").strip()
        country = (m.group("country") or "").strip()
        if city and city.casefold() not in _NOT_A_LOCATION:
            return f"{city} ({country})" if country else city
    head = tail.split(",")[0].strip()
    if head and not re.search(r"\d", head):
        return head
    return ""


def parse_habr(
    feed: dict[str, Any], *, source: str, queries: list[str] | None = None
) -> list[RawJob]:
    entries = feed.get("entries") or []
    needles = [q.casefold() for q in (queries or []) if q]
    jobs: list[RawJob] = []
    for e in entries:
        title = str(e.get("title") or "").strip()
        url = str(e.get("link") or "").strip()
        if not title or not url:
            continue
        summary = str(e.get("summary") or e.get("description") or "")
        blob = f"{title} {summary}"
        if needles and not any(n in blob.casefold() for n in needles):
            continue

        match = _ROLE_RE.search(title)
        tail = (match.group("tail") or "") if match else ""
        company = str(e.get("author") or "").strip()
        if not company:
            found = _COMPANY_RE.search(summary)
            company = found.group("company").strip() if found else "Хабр Карьера"
        low = blob.replace("ё", "е").casefold()
        salary_min, salary_max = parse_salary_rub(blob)

        jobs.append(
            RawJob(
                source=source,
                company=company,
                title=_clean_title(title),
                url=url,
                location=_location(summary, tail),
                # None = «неизвестно»: офисные объявления Хабра не должны
                # отсекаться жёстким remote-фильтром.
                remote=True if _REMOTE_RE.search(low) else None,
                salary_min=salary_min,
                salary_max=salary_max,
                currency="RUB" if salary_min is not None or salary_max is not None else "",
                description=summary,
                posted_at=_published(e),  # type: ignore[arg-type]
                raw={"id": e.get("id"), "skills": _SKILL_RE.findall(summary)[:20]},
            )
        )
    return jobs


class HabrSource(SourcePlugin):
    kind = "habr"

    async def fetch(self) -> list[RawJob]:
        url = self.cfg.url or FEED_URL
        body, status = await self.http.get_text(url)
        if body is None:
            raise self._fail("no data", status)
        feed = feedparser.parse(body)
        if not feed.get("entries"):
            raise self._fail("feed has no entries")
        return parse_habr(feed, source=self.name, queries=self.cfg.queries)


__all__ = ["HabrSource", "parse_habr", "parse_salary_rub", "FEED_URL"]
