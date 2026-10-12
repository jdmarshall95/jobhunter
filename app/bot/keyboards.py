"""Inline keyboards. Callback data is kept short (aiogram limit is 64 bytes)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import REJECT_REASONS, STATUS_ORDER

#: callback prefixes
CB_CARD = "c"  # c:<action>:<job id>
CB_REASON = "r"  # r:<reason slug>
CB_PAGE = "p"  # p:<list>:<page>
CB_STATUS = "s"  # s:<job id>:<status>
CB_FOLLOWUP = "f"  # f:<action>:<job id>

PAGE_SIZE = 10


def card_keyboard(job_id: str, *, saved: bool = False) -> InlineKeyboardMarkup:
    """🔗 Открыть · ⭐ Сохранить · ✅ Откликнулся · 📝 Черновик · ❌ Не интересно."""
    save_label = "⭐ Сохранено" if saved else "⭐ Сохранить"
    rows = [
        [
            InlineKeyboardButton(text="🔗 Открыть", url="{url}"),
            InlineKeyboardButton(text=save_label, callback_data=f"{CB_CARD}:s:{job_id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Откликнулся", callback_data=f"{CB_CARD}:a:{job_id}"),
            InlineKeyboardButton(text="📝 Черновик", callback_data=f"{CB_CARD}:d:{job_id}"),
            InlineKeyboardButton(text="❌ Не интересно", callback_data=f"{CB_CARD}:x:{job_id}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def with_url(kb: InlineKeyboardMarkup, url: str) -> InlineKeyboardMarkup:
    """Fill the placeholder of the URL button (aiogram needs a real URL)."""
    rows: list[list[InlineKeyboardButton]] = []
    for row in kb.inline_keyboard:
        rows.append(
            [
                InlineKeyboardButton(text=b.text, url=url) if b.text == "🔗 Открыть" else b
                for b in row
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reason_keyboard() -> InlineKeyboardMarkup:
    """FSM prompt after «❌ Не интересно» (ТЗ §7)."""
    rows: list[list[InlineKeyboardButton]] = []
    items = list(REJECT_REASONS.items())
    for i in range(0, len(items), 2):
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"{CB_REASON}:{slug}")
                for slug, label in items[i : i + 2]
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def status_keyboard(job_id: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(STATUS_ORDER), 3):
        rows.append(
            [
                InlineKeyboardButton(text=st, callback_data=f"{CB_STATUS}:{job_id}:{st}")
                for st in STATUS_ORDER[i : i + 3]
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def followup_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправил", callback_data=f"{CB_FOLLOWUP}:d:{job_id}"),
                InlineKeyboardButton(
                    text="⏳ Через 3 дня", callback_data=f"{CB_FOLLOWUP}:3:{job_id}"
                ),
                InlineKeyboardButton(text="🔒 Закрыть", callback_data=f"{CB_FOLLOWUP}:c:{job_id}"),
            ]
        ]
    )


def pages_keyboard(list_name: str, page: int, pages: int) -> InlineKeyboardMarkup | None:
    if pages <= 1:
        return None
    buttons: list[InlineKeyboardButton] = []
    if page > 1:
        buttons.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"{CB_PAGE}:{list_name}:{page - 1}")
        )
    buttons.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="noop"))
    if page < pages:
        buttons.append(
            InlineKeyboardButton(text="➡️", callback_data=f"{CB_PAGE}:{list_name}:{page + 1}")
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


__all__ = [
    "CB_CARD",
    "CB_FOLLOWUP",
    "CB_PAGE",
    "CB_REASON",
    "CB_STATUS",
    "PAGE_SIZE",
    "card_keyboard",
    "followup_keyboard",
    "pages_keyboard",
    "reason_keyboard",
    "status_keyboard",
    "with_url",
]
