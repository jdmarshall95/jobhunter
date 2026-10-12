"""Telegram handlers driven through a real aiogram Dispatcher (ТЗ §7).

Every test feeds an update into the router and asserts on the Telegram methods
recorded by :class:`tests.conftest.FakeSession`, so filters, FSM and keyboards
are exercised for real — no live connection needed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.bot.handlers import conversions, notify_new, resolve_job
from app.bot.keyboards import CB_CARD, CB_FOLLOWUP, CB_PAGE, CB_REASON, CB_STATUS, PAGE_SIZE
from app.pipeline.runner import RunSummary
from app.storage import Database
from tests.conftest import OWNER, StubLLM, StubPipeline, seed


def names(harness: Any) -> list[str]:
    return [type(m).__name__ for m in harness.session.calls]


async def make_pipeline(summary: RunSummary | None = None, draft: str | None = None) -> Any:
    return StubPipeline(summary=summary, llm=StubLLM(draft=draft))


class TestAccessControl:
    """ТЗ §2: the bot answers only the owner, everybody else gets silence."""

    async def test_owner_gets_an_answer(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/start")
        assert harness.texts and "Привет" in harness.texts[0]

    async def test_stranger_is_ignored_in_silence(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/start", chat_id=999)
        assert harness.session.calls == []

    async def test_empty_whitelist_answers_everybody(self, harness_factory: Any) -> None:
        harness = harness_factory(owners=set())
        await harness.command("/start", chat_id=999)
        assert harness.texts

    async def test_stranger_callback_is_ignored(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.callback("noop", chat_id=999)
        assert harness.answers == []


class TestStaticCommands:
    async def test_help_lists_the_commands(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/help")
        text = harness.texts[0]
        for token in ("/digest", "/stats", "/sources", "/export", "Черновик"):
            assert token in text

    async def test_unknown_command_is_not_answered(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/totally-unknown")
        assert harness.session.calls == []


class TestDigestAndLists:
    async def test_digest_shows_today(self, harness_factory: Any, db: Database) -> None:
        await seed(db, score=70.0)
        harness = harness_factory()
        await harness.command("/digest")
        text = harness.texts[0]
        assert "Дайджест" in text
        assert "новых за сутки: <b>1</b>" in text
        assert "Senior DevOps Engineer" in text

    async def test_digest_without_vacancies(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/digest")
        assert "ничего интересного" in harness.texts[0]

    async def test_digest_hides_suspicious(self, harness_factory: Any, db: Database) -> None:
        await seed(db, score=90.0, title="DevOps Engineer", description="fee", suspicious=True)
        harness = harness_factory()
        await harness.command("/digest")
        # the only vacancy is suspicious -> the 1-day query drops it, the 7-day
        # fallback also filters it out, so the digest stays empty.
        assert "ничего интересного" in harness.texts[0]

    async def test_new_marks_vacancies_as_shown(self, harness_factory: Any, db: Database) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.command("/new")
        assert "Новое" in harness.texts[0]
        row = await db.get_job(job_id)
        assert row["notified_at"] is not None

        harness.clear()
        await harness.command("/new")
        assert "Нового нет" in harness.texts[0]

    async def test_top_respects_the_argument(self, harness_factory: Any, db: Database) -> None:
        for i in range(3):
            await seed(db, company=f"Co{i}", title=f"DevOps {i}", url=f"https://t.io/{i}")
        harness = harness_factory()
        await harness.command("/top 2")
        assert "Топ-2 за 30 дней" in harness.texts[0]
        # header + exactly two rows
        assert len(harness.texts[0].strip().split("\n")) == 3

    async def test_suspicious_lists_only_scam_flags(
        self, harness_factory: Any, db: Database
    ) -> None:
        await seed(db, suspicious=True)
        await seed(db, company="PlainCo", title="SRE", url="https://p.io/1")
        harness = harness_factory()
        await harness.command("/suspicious")
        text = harness.texts[0]
        assert "Подозрительные" in text
        assert "CryptoCo" in text
        assert "PlainCo" not in text

    async def test_empty_funnel_list_says_pusto(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/saved")
        assert "пусто" in harness.texts[0]
        assert harness.sent[0].reply_markup is None


class TestFunnelLists:
    async def test_saved_only_lists_saved(self, harness_factory: Any, db: Database) -> None:
        kept = await seed(db)
        await seed(db, company="OtherCo", title="SRE", url="https://o.io/1")
        await db.set_status(kept, "saved")
        harness = harness_factory()
        await harness.command("/saved")
        text = harness.texts[0]
        assert "Сохранённые" in text
        assert "CryptoCo" in text
        assert "OtherCo" not in text

    async def test_applied_groups_the_post_apply_stages(
        self, harness_factory: Any, db: Database
    ) -> None:
        for i, status in enumerate(("applied", "screening", "tech", "offer")):
            job_id = await seed(
                db, company=f"C{i}", title=f"DevOps {i}", url=f"https://a.io/{i}"
            )
            await db.set_status(job_id, status)
        await seed(db, company="Fresh", title="SRE", url="https://a.io/fresh")
        harness = harness_factory()
        await harness.command("/applied")
        text = harness.texts[0]
        assert "Отклики" in text
        assert text.strip().count("\n") == 4  # header + four rows

    async def test_pagination_renders_keyboard_and_second_page(
        self, harness_factory: Any, db: Database
    ) -> None:
        for i in range(PAGE_SIZE + 1):
            await seed(
                db,
                company=f"Co{i:02}",
                title=f"DevOps {i:02}",
                url=f"https://pg.io/{i}",
                score=50.0,
            )
        harness = harness_factory()
        await harness.command("/pipeline")
        first = harness.sent[0]
        assert "стр. 1/2" in first.text
        assert first.reply_markup is not None

        harness.clear()
        await harness.callback(f"{CB_PAGE}:pipeline:2")
        edited = harness.edited[0]
        assert "стр. 2/2" in edited.text
        assert len(edited.text.strip().split("\n")) == 2  # header + the leftover row


class TestStatusCommand:
    async def test_no_arguments_explains_the_format(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/status")
        assert "Формат:" in harness.texts[0]

    async def test_unknown_id(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/status deadbeef")
        assert "Не нашёл вакансию" in harness.texts[0]

    async def test_bare_id_shows_current_status_with_keyboard(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.command(f"/status {job_id[:8]}")
        assert "Текущий статус: <b>new</b>" in harness.texts[0]
        assert harness.sent[0].reply_markup is not None

    async def test_unknown_status_name(self, harness_factory: Any, db: Database) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.command(f"/status {job_id} hired")
        assert "Не знаю статус" in harness.texts[0]

    async def test_status_is_persisted_and_logged(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.command(f"/status {job_id} tech")
        assert "Статус → <b>tech</b>" in harness.texts[0]
        row = await db.get_job(job_id)
        assert row["status"] == "tech"
        assert await db.count_events("status:tech", 1) == 1

    async def test_resolve_job_accepts_an_unambiguous_prefix(
        self, db: Database
    ) -> None:
        job_id = await seed(db)
        assert await resolve_job(db, job_id) is not None
        assert await resolve_job(db, job_id[:6]) is not None
        assert await resolve_job(db, "zzzz") is None


class TestAnalytics:
    async def test_stats_answers_for_two_windows(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/stats")
        assert len(harness.texts) == 2
        assert "за 7 дн." in harness.texts[0]
        assert "за 30 дн." in harness.texts[1]

    async def test_stats_renders_funnel_and_conversions(
        self, harness_factory: Any, db: Database
    ) -> None:
        applied = await seed(db, company="A", title="DevOps A", url="https://s.io/a")
        saved = await seed(db, company="B", title="DevOps B", url="https://s.io/b")
        await db.set_status(applied, "applied")
        await db.set_status(saved, "saved")
        harness = harness_factory()
        await harness.command("/stats")
        text = harness.texts[1]
        assert "всего вакансий: <b>2</b>" in text
        assert "new→saved" in text
        assert "saved→applied" in text

    def test_conversions_math(self) -> None:
        funnel = {"new": 5, "saved": 3, "applied": 1, "offer": 1}
        rates = conversions(funnel)
        assert rates["new→saved"] == pytest.approx(100.0 * 5 / 10)
        assert rates["saved→applied"] == pytest.approx(100.0 * 2 / 5)
        assert rates["applied→offer"] == pytest.approx(100.0 * 1 / 2)

    def test_conversions_of_an_empty_funnel(self) -> None:
        assert conversions({}) == {}

    async def test_sources_command(self, harness_factory: Any, db: Database) -> None:
        harness = harness_factory()
        await harness.command("/sources")
        assert "ни один ещё не отработал" in harness.texts[0]

        await db.record_source_ok("gh", 12)
        await db.record_source_error("lever", "SourceError: boom")
        harness.clear()
        await harness.command("/sources")
        text = harness.texts[0]
        assert "✅ gh" in text
        assert "❌ lever" in text
        assert "подряд ошибок 1" in text

    async def test_export_sends_a_csv_document(
        self, harness_factory: Any, db: Database
    ) -> None:
        await seed(db)
        harness = harness_factory()
        await harness.command("/export")
        assert "SendDocument" in names(harness)
        doc = harness.session.calls[0]
        assert doc.caption == "выгружено вакансий: 1"
        assert str(doc.document.filename).endswith(".csv")


class TestSettingsCommands:
    async def test_filters_echo_profile_and_blocklist(
        self, harness_factory: Any, db: Database
    ) -> None:
        await db.kv_set("blocklist", json.dumps(["Acme"]))
        harness = harness_factory()
        await harness.command("/filters")
        text = harness.texts[0]
        assert "devops, sre, platform engineer" in text
        assert "блок-лист компаний: Acme" in text
        assert "стоп-слова из отказов: —" in text

    async def test_block_requires_a_company(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/block")
        assert "Укажи компанию" in harness.texts[0]

    async def test_block_and_unblock_delegate_to_the_pipeline(
        self, harness_factory: Any
    ) -> None:
        pipeline = StubPipeline()
        harness = harness_factory(pipeline=pipeline)
        await harness.command("/block BadCo")
        assert "BadCo</b> в блок-листе" in harness.texts[0]
        await harness.command("/unblock BadCo")
        assert pipeline.blocked == ["BadCo"]
        assert pipeline.unblocked == ["BadCo"]

    async def test_channels_round_trip(self, harness_factory: Any, db: Database) -> None:
        harness = harness_factory()
        await harness.command("/addchannel @devops_ru")
        assert "каналы: devops_ru" in harness.texts[0]
        harness.clear()
        await harness.command("/addchannel kubernetes_ru")
        assert "devops_ru, kubernetes_ru" in harness.texts[0]
        harness.clear()
        await harness.command("/rmchannel devops_ru")
        assert "каналы: kubernetes_ru" in harness.texts[0]
        assert json.loads(await db.kv_get("channels")) == ["kubernetes_ru"]

    async def test_channel_command_requires_a_name(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.command("/addchannel")
        assert "Укажи канал" in harness.texts[0]

    async def test_pause_and_resume_toggle_the_kv_flag(
        self, harness_factory: Any, db: Database
    ) -> None:
        harness = harness_factory()
        await harness.command("/pause")
        assert "на паузе" in harness.texts[0]
        assert await db.kv_get("paused") == "1"
        harness.clear()
        await harness.command("/resume")
        assert "включены" in harness.texts[0]
        assert await db.kv_get("paused") == "0"

    async def test_run_reports_the_summary_and_pushes_cards(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        summary = RunSummary(new_ids=[job_id], accepted=1, raw_total=1)
        pipeline = StubPipeline(summary=summary)
        harness = harness_factory(pipeline=pipeline)
        await harness.command("/run")
        assert pipeline.run_calls == 1
        assert harness.texts[0].startswith("готово: ")
        assert "принято 1" in harness.texts[0]
        # the fresh card went out through the sender queue
        assert len(harness.ctx.sender.items) == 1


class TestCardCallbacks:
    async def test_save_button_stars_the_vacancy(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.callback(f"{CB_CARD}:s:{job_id}")
        row = await db.get_job(job_id)
        assert row["status"] == "saved"
        assert await db.count_events("saved", 1) == 1
        assert harness.edited, "the card must be edited in place"
        assert harness.answers[0].text == "сохранено ⭐"

    async def test_applied_button_answers_and_starts_the_clock(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.callback(f"{CB_CARD}:a:{job_id}")
        row = await db.get_job(job_id)
        assert row["status"] == "applied"
        assert row["followup_at"] is not None
        assert "Напомню через 6 дней" in harness.texts[0]

    async def test_draft_button_uses_the_llm(self, harness_factory: Any, db: Database) -> None:
        job_id = await seed(db)
        pipeline = StubPipeline(llm=StubLLM(draft="Готов писать вам по-русски."))
        harness = harness_factory(pipeline=pipeline)
        await harness.callback(f"{CB_CARD}:d:{job_id}")
        assert "Черновик отклика" in harness.texts[0]
        assert "Готов писать вам по-русски." in harness.texts[0]
        assert pipeline.llm.calls == 1

    async def test_draft_without_llm_falls_back_to_a_note(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory(pipeline=StubPipeline())
        await harness.callback(f"{CB_CARD}:d:{job_id}")
        assert "LLM недоступен" in harness.texts[0]

    async def test_unknown_job_answers_with_an_alert(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.callback(f"{CB_CARD}:s:ffffffffffffffff")
        assert harness.answers[0].show_alert is True
        assert harness.answers[0].text == "не нашёл"

    async def test_reject_flow_asks_for_a_reason(self, harness_factory: Any, db: Database) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.callback(f"{CB_CARD}:x:{job_id}")
        assert "Почему не подходит?" in harness.texts[0]
        assert harness.sent[0].reply_markup is not None

    async def test_reject_reason_stores_the_label(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.callback(f"{CB_CARD}:x:{job_id}")
        harness.clear()
        await harness.callback(f"{CB_REASON}:low_salary")
        row = await db.get_job(job_id)
        assert row["status"] == "rejected"
        assert row["reject_reason"] == "низкая вилка"
        assert "Убрал: низкая вилка" in harness.edited[0].text

    async def test_reject_reason_company_extends_the_blocklist(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        pipeline = StubPipeline()
        harness = harness_factory(pipeline=pipeline)
        await harness.callback(f"{CB_CARD}:x:{job_id}")
        await harness.callback(f"{CB_REASON}:company")
        assert pipeline.blocked == ["CryptoCo"]
        assert await db.kv_get("stopwords", "[]") == "[]"

    async def test_reject_reason_teaches_a_stopword(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.callback(f"{CB_CARD}:x:{job_id}")
        await harness.callback(f"{CB_REASON}:wrong_stack")
        stopwords = json.loads(await db.kv_get("stopwords"))
        assert stopwords == ["senior devops engineer"]

    async def test_fsm_is_cleared_after_a_reason(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.callback(f"{CB_CARD}:x:{job_id}")
        await harness.callback(f"{CB_REASON}:other")
        assert harness.edited[0].text == "Убрал: другое"
        harness.clear()
        # the state is gone, so a second click on the stale keyboard is inert
        await harness.callback(f"{CB_REASON}:other")
        assert harness.session.calls == []
        assert (await db.get_job(job_id))["status"] == "rejected"

    async def test_reason_without_a_pending_card(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.callback(f"{CB_REASON}:other")
        assert harness.session.calls == []

class TestStatusAndFollowupCallbacks:
    async def test_status_button(self, harness_factory: Any, db: Database) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.callback(f"{CB_STATUS}:{job_id}:screening")
        row = await db.get_job(job_id)
        assert row["status"] == "screening"
        assert "Статус → <b>screening</b>" in harness.edited[0].text

    async def test_malformed_status_callback_only_answers(
        self, harness_factory: Any
    ) -> None:
        harness = harness_factory()
        await harness.callback(f"{CB_STATUS}:onlytwo")
        assert harness.edited == []
        assert len(harness.answers) == 1

    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("d", "напомню ещё раз через 6 дней"),
            ("3", "вернусь через 3 дня"),
            ("c", "Закрыл"),
        ],
    )
    async def test_followup_buttons(
        self, harness_factory: Any, db: Database, action: str, expected: str
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        await harness.callback(f"{CB_FOLLOWUP}:{action}:{job_id}")
        assert expected.lower() in harness.edited[0].text.lower()
        row = await db.get_job(job_id)
        if action == "c":
            assert row["status"] == "ignored"
        else:
            assert row["followup_at"] is not None

    async def test_noop_button(self, harness_factory: Any) -> None:
        harness = harness_factory()
        await harness.callback("noop")
        assert len(harness.answers) == 1
        assert harness.edited == []


class TestNotifyNew:
    async def test_cards_are_pushed_and_marked(
        self, harness_factory: Any, db: Database
    ) -> None:
        job_id = await seed(db)
        harness = harness_factory()
        sent = await notify_new(harness.ctx, [job_id])
        assert sent == 1
        chat_id, text, markup = harness.ctx.sender.items[0]
        assert chat_id == OWNER
        assert "Senior DevOps Engineer" in text
        assert markup is not None
        assert (await db.get_job(job_id))["notified_at"] is not None

    async def test_paused_bot_pushes_nothing(self, harness_factory: Any, db: Database) -> None:
        job_id = await seed(db)
        await db.kv_set("paused", "1")
        harness = harness_factory()
        assert await notify_new(harness.ctx, [job_id]) == 0
        assert harness.ctx.sender.items == []

    async def test_empty_id_list_is_a_noop(self, harness_factory: Any) -> None:
        harness = harness_factory()
        assert await notify_new(harness.ctx, []) == 0

    async def test_per_run_cap(self, harness_factory: Any, db: Database) -> None:
        ids = [
            await seed(
                db,
                company=f"Co{i:02}",
                title=f"DevOps {i:02}",
                url=f"https://cap.io/{i}",
                score=50.0,
            )
            for i in range(12)
        ]
        harness = harness_factory()
        sent = await notify_new(harness.ctx, ids)
        assert sent == 10
        assert len(harness.ctx.sender.items) == 10

    async def test_missing_job_is_skipped(self, harness_factory: Any) -> None:
        harness = harness_factory()
        assert await notify_new(harness.ctx, ["ffffffffffffffff"]) == 0
