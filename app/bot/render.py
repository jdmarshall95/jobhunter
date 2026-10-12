"""HTML rendering for Telegram: cards, digest, stats.

Everything user-visible text goes through :func:`esc` — the ТЗ requires HTML
parse mode, so a vacancy title containing ``<`` must not break the message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html import escape
from typing import Any

from app.models import Job
from app.timeutil import to_local

#: Telegram hard limit for a text message.
TG_LIMIT = 4096

_STATUS_EMOJI = {
    "new": "🆕",
    "saved": "⭐",
    "applied": "✉️",
    "screening": "📞",
    "tech": "💻",
    "offer": "🏆",
    "rejected": "🚫",
    "ignored": "💤",
}


def esc(value: object) -> str:
    """Escape for ``parse_mode=HTML``."""
    return escape(str(value if value is not None else ""), quote=False)


@dataclass(slots=True)
class JobView:
    """Uniform read model: works from a DB row and from a live :class:`Job`."""

    id: str
    company: str
    title: str
    url: str
    location: str = ""
    salary_text: str = ""
    remote_text: str = ""
    score: float = 0.0
    breakdown: list[tuple[str, float]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    status: str = "new"
    suspicious: bool = False
    geo_restricted: bool = False
    llm_verdict: str = ""
    llm_summary: str = ""
    posted_text: str = ""


def _json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return [str(v) for v in data] if isinstance(data, list) else []
    return []


def _json_pairs(value: object) -> list[tuple[str, float]]:
    raw = _json_list(value)
    pairs: list[tuple[str, float]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]), float(item[1])))
    if pairs:
        return pairs
    # ``_json_list`` stringified the tuples; fall back to a raw decode.
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [(str(a), float(b)) for a, b in data if isinstance(data, list)]
    return []


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    try:  # sqlite3.Row
        keys = obj.keys()  # type: ignore[attr-defined]
    except AttributeError:
        return getattr(obj, name, default)
    return obj[name] if name in keys else default  # type: ignore[index]


def _as_bool(value: object) -> bool:
    return bool(value) and str(value) not in ("0", "None", "false")


def job_view(obj: Any) -> JobView:
    """Build a :class:`JobView` from a ``sqlite3.Row``, a dict or a :class:`Job`."""
    if isinstance(obj, Job):
        return JobView(
            id=obj.id,
            company=obj.company,
            title=obj.title,
            url=obj.url,
            location=obj.location,
            salary_text=obj.salary_text,
            remote_text=obj.remote_text,
            score=obj.score,
            breakdown=list(obj.score_breakdown),
            flags=list(obj.flags),
            sources=list(obj.sources) or [obj.source],
            status=obj.status,
            suspicious=obj.suspicious,
            geo_restricted=obj.geo_restricted,
            llm_verdict=obj.llm_verdict,
            llm_summary=obj.llm_summary,
        )

    remote = _get(obj, "remote")
    remote_text = ""
    if remote is not None:
        remote_text = {1: "remote", 0: "офис/гибрид"}.get(int(remote), "не указано")
    salary_min = _get(obj, "salary_min")
    salary_max = _get(obj, "salary_max")
    currency = str(_get(obj, "currency", "") or "")
    salary_text = ""
    if salary_min or salary_max:
        lo, hi = int(salary_min or 0), int(salary_max or 0)
        both = f"${lo:,}–${hi:,}".replace("\u2009", " ")
        salary_text = both if lo and hi != lo else f"${(hi or lo):,}"
        if currency:
            salary_text += f" {currency}"
    return JobView(
        id=str(_get(obj, "id", "")),
        company=str(_get(obj, "company", "")),
        title=str(_get(obj, "title", "")),
        url=str(_get(obj, "url", "")),
        location=str(_get(obj, "location", "") or ""),
        salary_text=salary_text,
        remote_text=remote_text,
        score=float(_get(obj, "score", 0.0) or 0.0),
        breakdown=_json_pairs(_get(obj, "score_breakdown", "[]")),
        flags=_json_list(_get(obj, "flags", "[]")),
        sources=_json_list(_get(obj, "sources", "[]")),
        status=str(_get(obj, "status", "new") or "new"),
        suspicious=_as_bool(_get(obj, "suspicious", 0)),
        geo_restricted=_as_bool(_get(obj, "geo_restricted", 0)),
        llm_verdict=str(_get(obj, "llm_verdict", "") or ""),
        llm_summary=str(_get(obj, "llm_summary", "") or ""),
    )


def score_bar(score: float) -> str:
    """Coarse 5-step bar so the score is readable at a glance."""
    filled = max(0, min(5, round(score / 20)))
    return "█" * filled + "░" * (5 - filled)


def breakdown_text(pairs: list[tuple[str, float]], limit: int = 6) -> str:
    if not pairs:
        return "—"
    ordered = sorted(pairs, key=lambda p: -abs(p[1]))[:limit]
    return "\n".join(f"{delta:+.0f} {esc(label)}" for label, delta in ordered)


def render_card(view: JobView, *, index: str = "", show_breakdown: bool = True) -> str:
    """One vacancy card (without buttons — those are an inline keyboard)."""
    head_bits = [f"{_STATUS_EMOJI.get(view.status, '')} <b>{esc(view.title)}</b>"]
    head = " ".join(head_bits)

    lines = [f"{index + ' ' if index else ''}{head}"]
    meta = [f"<b>{esc(view.company)}</b>"]
    if view.location:
        meta.append(esc(view.location))
    if view.remote_text:
        meta.append(esc(view.remote_text))
    lines.append(" · ".join(meta))

    if view.salary_text:
        lines.append(f"💰 {esc(view.salary_text)}")

    lines.append(f"📊 <b>{view.score:.0f}/100</b> {score_bar(view.score)}")
    if show_breakdown and view.breakdown:
        lines.append("Почему подошла:")
        lines.append(breakdown_text(view.breakdown))

    notes: list[str] = []
    if view.geo_restricted:
        notes.append("⚠️ гео-ограничение — уточни в переписке")
    if view.suspicious:
        notes.append("⚠️ похоже на скам-паттерн")
    for flag in view.flags:
        if flag not in ("suspicious",) and flag not in " ".join(notes):
            notes.append(f"ℹ️ {esc(flag)}")
    if notes:
        lines.append("\n".join(dict.fromkeys(notes)))

    if view.llm_summary:
        verdict = f" ({esc(view.llm_verdict)})" if view.llm_verdict else ""
        lines.append(f"🤖 ИИ-вердикт{verdict}: {esc(view.llm_summary)}")

    if len(view.sources) > 1:
        lines.append(f"🔁 найдена в {len(view.sources)} источниках: {esc(', '.join(view.sources))}")
    elif view.sources:
        lines.append(f"Источник: {esc(view.sources[0])}")

    return "\n".join(lines)


def render_list(views: list[JobView], title: str, *, page: int = 1, pages: int = 1) -> str:
    """Compact numbered list used by /saved, /applied, /pipeline."""
    if not views:
        return f"<b>{esc(title)}</b>\nпусто"
    header = f"<b>{esc(title)}</b>" + (f" · стр. {page}/{pages}" if pages > 1 else "")
    body = [
        (
            f"{i}. <b>{v.score:.0f}</b> {esc(v.title)} — {esc(v.company)}"
            f" <i>{_STATUS_EMOJI.get(v.status, '')}</i>"
        )
        for i, v in enumerate(views, start=1)
    ]
    return "\n".join([header, *body])


def render_digest(views: list[JobView], *, date_text: str, new_count: int) -> str:
    head = f"🌅 <b>Дайджест {esc(date_text)}</b>\nновых за сутки: <b>{new_count}</b>"
    if not views:
        return f"{head}\n\nСегодня ничего интересного — пороги скоринга пока никто не взял."
    items = [
        f"{i}. <b>{v.score:.0f}</b> <a href=\"{esc(v.url)}\">{esc(v.title)}</a> — {esc(v.company)}"
        + (f" · {esc(v.salary_text)}" if v.salary_text else "")
        for i, v in enumerate(views, start=1)
    ]
    return "\n".join([head, "", *items])


def render_stats(
    *,
    funnel: dict[str, int],
    conversions: dict[str, float],
    best_sources: list[tuple[str, int, float]],
    window_days: int,
) -> str:
    lines = [f"📈 <b>Статистика за {window_days} дн.</b>", ""]
    total = sum(funnel.values())
    lines.append(f"всего вакансий: <b>{total}</b>")
    for status in ("new", "saved", "applied", "screening", "tech", "offer", "rejected", "ignored"):
        count = funnel.get(status, 0)
        if count:
            lines.append(f"{_STATUS_EMOJI.get(status, '')} {status}: {count}")
    if conversions:
        lines.append("")
        for label, value in conversions.items():
            lines.append(f"{esc(label)}: <b>{value:.0f}%</b>")
    if best_sources:
        lines.append("")
        lines.append("лучшие источники:")
        for name, count, avg in best_sources[:5]:
            lines.append(f"· {esc(name)} — {count} шт., средний {avg:.0f}")
    return "\n".join(lines)


def render_sources(rows: list[Any], *, tz_name: str | None = None) -> str:
    """Renders :class:`app.storage.db.SourceHealth` records (ТЗ §7 ``/sources``).

    Timestamps are stored in UTC and shown in ``tz_name`` so the bot does not
    contradict the scheduler's clock.
    """
    if not rows:
        return "🧭 <b>Источники</b>\nни один ещё не отработал"
    lines = ["🧭 <b>Источники</b>", ""]
    for row in rows:
        name = str(_get(row, "name", ""))
        ok = _as_bool(_get(row, "ok", False))
        runs = int(_get(row, "runs", 0) or 0)
        found = int(_get(row, "jobs_found", 0) or 0)
        errors = int(_get(row, "consecutive_errors", 0) or 0)
        last = to_local(_get(row, "last_ok_at", ""), tz_name)
        mark = "✅" if ok else "❌"
        line = f"{mark} {esc(name)} · запусков {runs} · найдено {found} · последняя {last}"
        if errors:
            line += f" · подряд ошибок {errors}"
        err = str(_get(row, "last_error", "") or "")
        if err and not ok:
            line += f"\n   <i>{esc(err[:120])}</i>"
        lines.append(line)
    return "\n".join(lines)


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Split long text on line boundaries (Telegram 4096-char limit)."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        extra = len(line) + 1
        if size + extra > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


__all__ = [
    "TG_LIMIT",
    "JobView",
    "breakdown_text",
    "esc",
    "job_view",
    "render_card",
    "render_digest",
    "render_list",
    "render_sources",
    "render_stats",
    "score_bar",
    "split_message",
]
