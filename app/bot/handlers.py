"""Telegram handlers (aiogram 3).

The bot answers *only* the owner: everything else is ignored in silence (ТЗ §2).
Commands are thin wrappers over small helpers so that unit tests can assert on
the produced text without a live Telegram connection.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards import (
    CB_CARD,
    CB_FOLLOWUP,
    CB_PAGE,
    CB_REASON,
    CB_STATUS,
    PAGE_SIZE,
    card_keyboard,
    pages_keyboard,
    reason_keyboard,
    status_keyboard,
    with_url,
)
from app.bot.render import (
    esc,
    job_view,
    render_card,
    render_digest,
    render_list,
    render_sources,
    render_stats,
)
from app.bot.sender import Sender
from app.config import Profile, Settings
from app.logging_setup import get_logger
from app.models import REJECT_REASONS, STATUS_ORDER
from app.pipeline.runner import Pipeline
from app.storage import Database
from app.timeutil import now_in

log = get_logger(__name__)

KV_PAUSED = "paused"
KV_CHANNELS = "channels"
#: how many new cards a single pipeline run may push
MAX_NOTIFY_PER_RUN = 10
#: ``/top`` accepts ``/top 20``, ``/top_20``, ``/top-20``, ``/top20`` — but not
#: ``/totally-unknown``, so the tail must not continue with letters.
TOP_PATTERN = re.compile(r"^top(?![a-z])")


class RejectStates(StatesGroup):
    """FSM: «❌ Не интересно» → question about the reason."""

    waiting = State()


@dataclass
class BotCtx:
    """Everything the handlers need (one instance per process)."""

    settings: Settings
    profile: Profile
    db: Database
    pipeline: Pipeline
    sender: Sender
    owners: set[int] = field(default_factory=set)
    api: Any = None

    def is_owner(self, chat_id: int | None) -> bool:
        if not self.owners:
            return True  # no whitelist configured -> answer everybody
        return chat_id in self.owners

    async def paused(self) -> bool:
        return (await self.db.kv_get(KV_PAUSED, "0")) == "1"


# --------------------------------------------------------------------- helpers
async def resolve_job(db: Database, token: str) -> Any | None:
    """Find a vacancy by full id or by an unambiguous id prefix."""
    row = await db.get_job(token)
    if row is not None:
        return row
    rows = await db.list_jobs(limit=500)
    matches = [r for r in rows if str(r["id"]).startswith(token)]
    return matches[0] if len(matches) == 1 else None


def _col(row: Any, name: str, default: str = "") -> str:
    """Safe column read: ``sqlite3.Row`` raises ``IndexError`` on absent columns."""
    try:
        value = row[name]
    except (IndexError, KeyError, TypeError):
        return default
    return str(value) if value is not None else default


def conversions(funnel: dict[str, int]) -> dict[str, float]:
    """Funnel conversion rates (saved→applied, applied→offer)."""
    out: dict[str, float] = {}
    saved = funnel.get("saved", 0)
    applied = sum(funnel.get(k, 0) for k in ("applied", "screening", "tech", "offer"))
    total = sum(funnel.values())
    if total:
        out["new→saved"] = 100.0 * (saved + applied) / total
    if saved + applied:
        out["saved→applied"] = 100.0 * applied / (saved + applied)
    if applied:
        out["applied→offer"] = 100.0 * funnel.get("offer", 0) / applied
    return out


async def top_rows(
    db: Database,
    *,
    limit: int,
    statuses: list[str] | None = None,
    since_days: int | None = 30,
    only_unnotified: bool = False,
    include_suspicious: bool | None = False,
) -> list[Any]:
    return await db.list_jobs(
        statuses=statuses,
        since_days=since_days,
        only_unnotified=only_unnotified,
        include_suspicious=include_suspicious,
        limit=limit,
    )


async def send_card(ctx: BotCtx, row: Any, index: str = "") -> None:
    view = job_view(row)
    text = render_card(view, index=index)
    kb = with_url(card_keyboard(view.id, saved=view.status != "new"), view.url)
    chat_id = int(_col(row, "chat_id") or _chat(ctx))
    ctx.sender.send(chat_id, text, reply_markup=kb)


def _chat(ctx: BotCtx) -> int:
    ids = sorted(ctx.owners)
    return ids[0] if ids else 0


async def notify_new(ctx: BotCtx, job_ids: list[str]) -> int:
    """Push cards for freshly found vacancies (idempotent via ``notified_at``)."""
    if not job_ids or await ctx.paused():
        return 0
    sent = 0
    for job_id in job_ids[:MAX_NOTIFY_PER_RUN]:
        row = await ctx.db.get_job(job_id)
        if row is None:
            continue
        await send_card(ctx, row)
        sent += 1
    await ctx.db.mark_notified(job_ids)
    return sent


# --------------------------------------------------------------------- router
def build_router(ctx: BotCtx) -> Router:
    router = Router(name="jobhunter")

    async def owner_only(message: Message) -> bool:
        if ctx.is_owner(message.chat.id):
            return True
        log.info("ignored message from non-owner chat=%s", message.chat.id)
        return False

    async def cb_owner_only(cb: CallbackQuery) -> bool:
        chat_id = cb.message.chat.id if cb.message else None
        return ctx.is_owner(chat_id)

    router.message.filter(owner_only)
    router.callback_query.filter(cb_owner_only)

    # ------------------------------------------------------------ /start
    @router.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "Привет! Я охочусь за вакансиями DevOps/SRE в крипте.\n"
            "Напиши /digest — покажу лучшее за сегодня, /help — полный список команд."
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "<b>Команды</b>\n"
            "/digest — топ-10 дайджест\n"
            "/new — не показанные ранее\n"
            "/top [N] — лучшие за 30 дней\n"
            "/saved, /applied, /pipeline — воронка (с пагинацией)\n"
            "/status <id> <статус> — сменить статус\n"
            "/stats — метрики за 7 и 30 дней\n"
            "/sources — здоровье источников\n"
            "/filters — текущие фильтры и блок-лист\n"
            "/block <компания>, /unblock <компания>\n"
            "/addchannel <имя>, /rmchannel <имя>\n"
            "/suspicious — подозрительные (скам-паттерн)\n"
            "/export — выгрузить CSV\n"
            "/pause, /resume — пауза уведомлений\n"
            "/run — запустить сбор вручную\n"
            "\nКнопки карточки: 🔗 Открыть · ⭐ Сохранить · ✅ Откликнулся · "
            "📝 Черновик · ❌ Не интересно"
        )

    # ------------------------------------------------------------ lists
    @router.message(Command("digest"))
    async def cmd_digest(message: Message) -> None:
        rows = await top_rows(ctx.db, limit=10, since_days=1, include_suspicious=False)
        if not rows:
            rows = await top_rows(ctx.db, limit=10, since_days=7, include_suspicious=False)
        new_count = await ctx.db.count_jobs(since_days=1)
        text = render_digest(
            [job_view(r) for r in rows],
            date_text=now_in(ctx.settings.timezone).strftime("%d.%m"),
            new_count=new_count,
        )
        await message.answer(text)

    @router.message(Command("new"))
    async def cmd_new(message: Message) -> None:
        rows = await ctx.db.list_jobs(only_unnotified=True, limit=10, include_suspicious=None)
        if not rows:
            await message.answer("Нового нет — всё уже показывал.")
            return
        await message.answer(render_list([job_view(r) for r in rows], "Новое", page=1, pages=1))
        await ctx.db.mark_notified([str(r["id"]) for r in rows])

    @router.message(Command(TOP_PATTERN))
    async def cmd_top(message: Message, command: CommandObject) -> None:
        limit = _int_arg(_top_argument(command), 10)
        rows = await top_rows(ctx.db, limit=limit, since_days=30, include_suspicious=None)
        await message.answer(render_list([job_view(r) for r in rows], f"Топ-{limit} за 30 дней"))

    @router.message(Command("suspicious"))
    async def cmd_suspicious(message: Message) -> None:
        rows = await ctx.db.list_jobs(include_suspicious=True, limit=10)
        await message.answer(render_list([job_view(r) for r in rows], "Подозрительные"))

    @router.message(Command("saved", "applied", "pipeline"))
    async def cmd_funnel(message: Message, command: CommandObject) -> None:
        name = command.command or "pipeline"
        statuses, title = _funnel_query(name)
        page = _int_arg(command.args, 1)
        rows = await ctx.db.list_jobs(
            statuses=statuses,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
            include_suspicious=None,
        )
        total = await ctx.db.count_jobs(statuses=statuses)
        pages = max(1, -(-total // PAGE_SIZE))
        await message.answer(
            render_list([job_view(r) for r in rows], title, page=page, pages=pages),
            reply_markup=pages_keyboard(name, page, pages),
        )

    @router.message(Command("status"))
    async def cmd_status(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        if not args:
            await message.answer("Формат: /status <id> <статус>")
            return
        row = await resolve_job(ctx.db, args[0])
        if row is None:
            await message.answer(f"Не нашёл вакансию по id «{esc(args[0])}»")
            return
        if len(args) < 2:
            await message.answer(
                f"Текущий статус: <b>{esc(row['status'])}</b>",
                reply_markup=status_keyboard(str(row["id"])),
            )
            return
        new_status = args[1].lower()
        if new_status not in STATUS_ORDER:
            known = ", ".join(STATUS_ORDER)
            await message.answer(f"Не знаю статус «{esc(new_status)}». Есть: {esc(known)}")
            return
        await ctx.db.set_status(str(row["id"]), new_status)
        await ctx.db.log_event(str(row["id"]), f"status:{new_status}")
        await message.answer(f"Статус → <b>{esc(new_status)}</b>")

    # ------------------------------------------------------------ analytics
    @router.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        for days in (7, 30):
            funnel = await ctx.db.funnel_counts(since_days=days)
            sources = await ctx.db.top_sources(since_days=days)
            await message.answer(
                render_stats(
                    funnel=funnel,
                    conversions=conversions(funnel),
                    best_sources=sources,
                    window_days=days,
                )
            )

    @router.message(Command("sources"))
    async def cmd_sources(message: Message) -> None:
        rows = await ctx.db.source_health_all()
        await message.answer(render_sources(rows, tz_name=ctx.settings.timezone))

    @router.message(Command("filters"))
    async def cmd_filters(message: Message) -> None:
        f = ctx.profile.filters
        blocklist = json.loads(await ctx.db.kv_get("blocklist", "[]") or "[]")
        stopwords = json.loads(await ctx.db.kv_get("stopwords", "[]") or "[]")
        await message.answer(
            "<b>Фильтры</b>\n"
            f"include: {esc(', '.join(f.include_keywords))}\n"
            f"exclude: {esc(', '.join(f.exclude_keywords))}\n"
            f"блок-лист компаний: {esc(', '.join(blocklist) or '—')}\n"
            f"стоп-слова из отказов: {esc(', '.join(stopwords) or '—')}\n"
            f"вилка: {esc(ctx.profile.candidate.salary_expectation_usd_month.describe())}\n"
            f"роль строго: {'да' if f.role_strict else 'нет (флаг + штраф)'}\n"
            f"мин. скор: {ctx.profile.min_score} · алерт: {ctx.profile.alert_score}"
        )

    @router.message(Command("block", "unblock"))
    async def cmd_block(message: Message, command: CommandObject) -> None:
        company = (command.args or "").strip()
        if not company:
            await message.answer("Укажи компанию: /block Example")
            return
        if command.command == "block":
            await ctx.pipeline.add_block_company(company)
            await ctx.db.log_event(None, "block", company)
            await message.answer(f"<b>{esc(company)}</b> в блок-листе")
        else:
            await ctx.pipeline.remove_block_company(company)
            await ctx.db.log_event(None, "unblock", company)
            await message.answer(f"<b>{esc(company)}</b> убрана из блок-листа")

    @router.message(Command("addchannel", "rmchannel"))
    async def cmd_channel(message: Message, command: CommandObject) -> None:
        name = (command.args or "").strip().lstrip("@")
        if not name:
            await message.answer("Укажи канал: /addchannel devops_ru")
            return
        channels = json.loads(await ctx.db.kv_get(KV_CHANNELS, "[]") or "[]")
        if command.command == "addchannel":
            if name not in channels:
                channels.append(name)
        else:
            channels = [c for c in channels if c != name]
        await ctx.db.kv_set(KV_CHANNELS, json.dumps(channels, ensure_ascii=False))
        await message.answer("каналы: " + (", ".join(channels) or "—"))

    @router.message(Command("pause", "resume"))
    async def cmd_pause(message: Message) -> None:
        value = "1" if message.text and message.text.startswith("/pause") else "0"
        await ctx.db.kv_set(KV_PAUSED, value)
        note = "уведомления на паузе 🔕" if value == "1" else "уведомления включены 🔔"
        await message.answer(note)

    @router.message(Command("export"))
    async def cmd_export(message: Message) -> None:
        rows = await ctx.db.list_jobs(limit=1000, include_suspicious=None)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["id", "score", "status", "company", "title", "location", "salary", "url", "sources"]
        )
        for row in rows:
            view = job_view(row)
            writer.writerow(
                [
                    view.id[:8],
                    round(view.score),
                    view.status,
                    view.company,
                    view.title,
                    view.location,
                    view.salary_text,
                    view.url,
                    "|".join(view.sources),
                ]
            )
        payload = buf.getvalue().encode("utf-8-sig")  # BOM: Excel opens UTF-8 correctly
        filename = f"jobs_{now_in(ctx.settings.timezone):%Y%m%d}.csv"
        if ctx.api is not None:
            await ctx.api.send_document(
                chat_id=message.chat.id,
                document=BufferedInputFile(payload, filename=filename),
                caption=f"выгружено вакансий: {len(rows)}",
            )
        else:  # pragma: no cover - tests without an api handle
            await message.answer(f"выгружено вакансий: {len(rows)}")

    @router.message(Command("run"))
    async def cmd_run(message: Message) -> None:
        summary = await ctx.pipeline.run()
        await message.answer(f"готово: {summary.as_text()}")
        await notify_new(ctx, summary.new_ids)

    # ------------------------------------------------------------ callbacks
    @router.callback_query(F.data.startswith(f"{CB_CARD}:"))
    async def cb_card(cb: CallbackQuery, state: FSMContext) -> None:
        _, _, job_id = (cb.data or "").split(":", 2)
        row = await ctx.db.get_job(job_id)
        if row is None or cb.message is None:
            await cb.answer("не нашёл", show_alert=True)
            return
        action = _card_action(cb.data or "")
        if action == "s":
            await ctx.db.set_status(job_id, "saved")
            await ctx.db.log_event(job_id, "saved")
            await cb.message.edit_text(render_card(job_view(row), show_breakdown=True))
            await cb.answer("сохранено ⭐")
        elif action == "a":
            await ctx.db.set_status(job_id, "applied")
            await ctx.db.log_event(job_id, "applied")
            await cb.message.answer("Отметил: отклик отправлен ✉️ Напомню через 6 дней.")
            await cb.answer()
        elif action == "d":
            await cb.answer("генерирую черновик…")
            draft = await ctx.pipeline.llm.draft(job_view_to_job(row), ctx.profile)
            text = draft or (
                "LLM недоступен (нет ключа или выбран лимит дней). "
                "Черновик можно собрать вручную из блока facts в config/profile.yaml."
            )
            await cb.message.answer(f"📝 <b>Черновик отклика</b>\n{text}")
        elif action == "x":
            await state.set_state(RejectStates.waiting)
            await state.update_data(reject_job=job_id)
            await cb.message.answer("Почему не подходит?", reply_markup=reason_keyboard())
            await cb.answer()
        else:  # pragma: no cover - defensive
            await cb.answer()

    @router.callback_query(RejectStates.waiting, F.data.startswith(f"{CB_REASON}:"))
    async def cb_reason(cb: CallbackQuery, state: FSMContext) -> None:
        slug = (cb.data or "").split(":", 1)[1]
        pending = await _pending_reject(ctx, state)
        label = REJECT_REASONS.get(slug, slug)
        if pending is not None:
            await ctx.db.set_status(pending, "rejected", reason=label)
            await ctx.db.log_event(pending, "reject", label)
            await _learn_stopword(ctx, slug, pending)
        await state.clear()
        if cb.message:
            await cb.message.edit_text(f"Убрал: {esc(label)}")
        await cb.answer()

    @router.callback_query(F.data.startswith(f"{CB_STATUS}:"))
    async def cb_status(cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        if len(parts) == 3:
            _, job_id, new_status = parts
            await ctx.db.set_status(job_id, new_status)
            await ctx.db.log_event(job_id, f"status:{new_status}")
            if cb.message:
                await cb.message.edit_text(f"Статус → <b>{esc(new_status)}</b>")
        await cb.answer()

    @router.callback_query(F.data.startswith(f"{CB_FOLLOWUP}:"))
    async def cb_followup(cb: CallbackQuery) -> None:
        _, action, job_id = (cb.data or "").split(":", 2)
        if action == "d":
            await ctx.db.set_followup(job_id, days=6)
            text = "Ок, напомню ещё раз через 6 дней."
        elif action == "3":
            await ctx.db.set_followup(job_id, days=3)
            text = "Ок, вернусь через 3 дня."
        else:
            await ctx.db.set_status(job_id, "ignored")
            text = "Закрыл 💤"
        if cb.message:
            await cb.message.edit_text(text)
        await cb.answer()

    @router.callback_query(F.data.startswith(f"{CB_PAGE}:"))
    async def cb_page(cb: CallbackQuery) -> None:
        _, name, page_raw = (cb.data or "").split(":", 2)
        page = int(page_raw or 1)
        statuses, title = _funnel_query(name)
        rows = await ctx.db.list_jobs(
            statuses=statuses,
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
            include_suspicious=None,
        )
        total = await ctx.db.count_jobs(statuses=statuses)
        pages = max(1, -(-total // PAGE_SIZE))
        if cb.message:
            await cb.message.edit_text(
                render_list([job_view(r) for r in rows], title, page=page, pages=pages),
                reply_markup=pages_keyboard(name, page, pages),
            )
        await cb.answer()

    @router.callback_query(F.data == "noop")
    async def cb_noop(cb: CallbackQuery) -> None:
        await cb.answer()

    return router


# ------------------------------------------------------- callback small print
def _card_action(data: str) -> str:
    parts = data.split(":")
    return parts[1] if len(parts) > 1 else ""


def _int_arg(value: str | None, default: int) -> int:
    try:
        return max(1, int((value or "").strip()))
    except ValueError:
        return default


def _top_argument(command: CommandObject | None) -> str | None:
    """Pull the ``N`` out of ``/top 20`` as well as out of ``/top_20``.

    aiogram tokenises ``/top_20`` as a command named ``top_20`` (the digits are
    part of the command, not ``args``), so the number has to be scraped from the
    command name first and only then read from ``args``.
    """
    if command is None:
        return None
    digits = re.sub(r"\D", "", command.command or "")
    return digits or (command.args or None)


def _funnel_query(name: str) -> tuple[list[str] | None, str]:
    if name == "saved":
        return ["saved"], "Сохранённые"
    if name == "applied":
        return ["applied", "screening", "tech", "offer"], "Отклики"
    return None, "Воронка"


async def _pending_reject(ctx: BotCtx, state: FSMContext) -> str | None:
    """The job id the owner was just asked about (stored in FSM data)."""
    data = await state.get_data()
    job_id = data.get("reject_job")
    if job_id:
        await state.update_data(reject_job=None)
    return job_id or None


async def _learn_stopword(ctx: BotCtx, slug: str, job_id: str) -> None:
    """Reject feedback: «компания не нравится» extends the block-list, other
    reasons add a soft stop-word so similar titles score lower next time."""
    row = await ctx.db.get_job(job_id)
    if row is None:
        return
    company = str(row["company"])
    if slug == "company":
        await ctx.pipeline.add_block_company(company)
        return
    stopwords = json.loads(await ctx.db.kv_get("stopwords", "[]") or "[]")
    token = str(row["title"]).split("(")[0].strip().lower()
    if token and token not in stopwords and len(stopwords) < 30:
        stopwords.append(token)
        await ctx.db.kv_set("stopwords", json.dumps(stopwords, ensure_ascii=False))


def job_view_to_job(row: Any) -> Any:
    """Minimal :class:`app.models.Job` reconstruction for the LLM draft prompt."""
    from app.models import Job

    return Job(
        source=_col(row, "source", "db"),
        company=str(row["company"]),
        title=str(row["title"]),
        url=str(row["url"]),
        location=str(row["location"] or ""),
        description=str(row["description"] or ""),
    )


__all__ = [
    "BotCtx",
    "RejectStates",
    "build_router",
    "conversions",
    "notify_new",
    "resolve_job",
    "send_card",
]
