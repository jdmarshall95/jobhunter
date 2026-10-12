"""Pipeline orchestration: fetch isolation, counters, dry-run, blocklist (ТЗ §6)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import httpx
import pytest

from app.config import Profile, SourceConfig
from app.llm import LLMResult, LLMService
from app.models import RawJob
from app.pipeline import Pipeline, RunSummary
from app.pipeline.runner import KV_BLOCKLIST, KV_CHANNELS, SourceResult
from app.sources.base import SourceError
from app.sources.telegram import TelegramSource
from app.storage import Database
from tests.conftest import StubLLM, StubSource, make_fetcher, make_raw, stub_source

#: Scores 68.5 with the ``profile`` fixture (passes ``min_score``, below ``alert_score``).
GOOD_DESC = (
    "We are a crypto exchange looking for a Senior DevOps Engineer. "
    "Kubernetes, Terraform, Ansible and Prometheus. Russian-speaking team. "
    "$4,200 monthly, fully remote."
)

#: Scores 82.0 — the ceiling reachable with the four core/four plus tokens.
RICH_DESC = (
    "Senior SRE for a crypto exchange. Kubernetes, Terraform, Ansible, GitLab CI, Helm, "
    "Prometheus, Grafana, AWS. Russian-speaking team. $5,000 monthly, fully remote."
)

ONSITE_RAW = make_raw(
    title="Junior Python Developer",
    url="https://b.io/jobs/2",
    location="Warsaw, Poland",
    description="Python backend in a fintech. On-site, Kubernetes.",
)
BLOCKED_RAW = make_raw(
    company="Acme Corp",
    title="DevOps Engineer",
    url="https://c.io/jobs/3",
    location="Remote",
    description="DevOps with Kubernetes.",
)


class CountingLLM:
    """LLM stand-in that reports a result, so ``llm_calls`` actually increments."""

    def __init__(self, result: Any = "result") -> None:
        self._result = result
        self.seen: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def rescore(self, job: Any, profile: Profile) -> Any:
        self.seen.append(job.id)
        return self._result

    async def draft(self, job: Any, profile: Profile, lang: str = "ru") -> str:
        return "черновик"


class RecordingLLMClient:
    """Gemini stand-in whose ``score()`` always answers with the same verdict."""

    async def score(self, job: Any, profile: Profile) -> tuple[Any, float]:
        return (
            LLMResult(verdict="fit", score=91, summary="Отличный мэтч.", red_flags=[]),
            0.01,
        )

    async def aclose(self) -> None:
        pass


class ClosingClient:
    """Records ``aclose()`` so :meth:`Pipeline.close` can be asserted."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class SlowSource(StubSource):
    """Stub that tracks how many instances are inside ``fetch()`` concurrently."""

    tracker: ClassVar[dict[str, int]] = {"live": 0, "peak": 0}

    async def fetch(self) -> list[RawJob]:
        tracker = type(self).tracker
        tracker["live"] += 1
        tracker["peak"] = max(tracker["peak"], tracker["live"])
        try:
            await asyncio.sleep(0.01)
        finally:
            tracker["live"] -= 1
        return list(self._jobs)


def slow_source(name: str) -> SlowSource:
    cfg = SourceConfig(name=name, kind="greenhouse", token=name)
    return SlowSource(cfg, make_fetcher(lambda request: None))


def good_raw(**kwargs: Any) -> RawJob:
    base: dict[str, Any] = {
        "location": "Remote (EMEA)",
        "description": GOOD_DESC,
    }
    base.update(kwargs)
    return make_raw(**base)


@pytest.fixture
def pipeline_factory(settings: Any, db: Database, profile: Profile) -> Any:
    """Build a pipeline over stub sources; keyword args patch the profile."""

    def _make(*sources: Any, llm: Any = None, **profile_updates: Any) -> Pipeline:
        prof = profile.model_copy(update=profile_updates) if profile_updates else profile
        return Pipeline(
            settings,
            prof,
            db,
            http=make_fetcher(lambda request: httpx.Response(200, json={})),
            llm=llm or StubLLM(),
            sources=list(sources),
        )

    return _make


class TestFetchAll:
    async def test_healthy_sources_are_recorded(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        pipe = pipeline_factory(
            stub_source("gh", [good_raw()]),
            stub_source("ashby", [good_raw(source="ashby", url="https://d.io/9")]),
        )
        raw, results = await pipe.fetch_all()

        assert len(raw) == 2
        assert [r.name for r in results] == ["gh", "ashby"]
        assert all(r.ok for r in results)
        assert [r.jobs for r in results] == [1, 1]

        health = {h.name: h for h in await db.source_health_all()}
        assert health["gh"].ok is True
        assert health["ashby"].jobs_found == 1

    async def test_failing_source_is_isolated(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        pipe = pipeline_factory(
            stub_source("gh", [good_raw()]),
            stub_source("lever", error=SourceError("boom")),
        )
        raw, results = await pipe.fetch_all()

        by_name = {r.name: r for r in results}
        assert by_name["gh"].ok is True
        assert by_name["lever"].ok is False
        assert by_name["lever"].error == "SourceError: boom"
        assert by_name["lever"].jobs == 0
        # the healthy source still contributes its vacancies
        assert [job.source for job in raw] == ["gh"]

        health = {h.name: h for h in await db.source_health_all()}
        assert health["lever"].ok is False
        assert health["lever"].consecutive_errors == 1
        assert "boom" in health["lever"].last_error

    async def test_concurrency_is_bounded(
        self, pipeline_factory: Any, settings: Any
    ) -> None:
        SlowSource.tracker = {"live": 0, "peak": 0}
        pipe = pipeline_factory(*(slow_source(f"s{i}") for i in range(4)))

        await pipe.fetch_all()

        assert settings.fetch_concurrency == 2
        assert SlowSource.tracker["peak"] == 2

    async def test_no_sources_yields_empty_result(self, pipeline_factory: Any) -> None:
        raw, results = await pipeline_factory().fetch_all()

        assert raw == []
        assert results == []


class TestRunCounters:
    async def test_counters_across_every_stage(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        pipe = pipeline_factory(
            stub_source(
                "gh",
                [
                    good_raw(),
                    good_raw(source="ashby"),  # same vacancy, second source
                    ONSITE_RAW,
                    BLOCKED_RAW,
                ],
            )
        )
        summary = await pipe.run()

        assert summary.raw_total == 4
        assert summary.duplicates == 1
        assert summary.filtered == 2  # onsite + blocked company
        assert summary.accepted == 1
        assert summary.new_ids and summary.ok_sources == 1

        assert await db.count_jobs() == 1
        assert await db.count_events("new", 1) == 1

    async def test_one_vacancy_from_two_sources_is_one_card(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        pipe = pipeline_factory(
            stub_source("gh", [good_raw()]),
            stub_source("ashby", [good_raw(source="ashby")]),
        )
        summary = await pipe.run()

        assert await db.count_jobs() == 1
        assert len(summary.new_ids) == 1

        row = await db.get_job(summary.new_ids[0])
        assert json.loads(row["sources"]) == ["gh", "ashby"]
        assert row["score"] == pytest.approx(68.5)

    async def test_second_run_adds_nothing_new(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        sources = lambda: [stub_source("gh", [good_raw()])]  # noqa: E731
        first = await pipeline_factory(*sources()).run()
        second = await pipeline_factory(*sources()).run()

        assert len(first.new_ids) == 1
        assert second.new_ids == []
        assert second.accepted == 1
        assert await db.count_jobs() == 1
        assert await db.count_events("new", 1) == 1

    async def test_dry_run_writes_nothing(self, pipeline_factory: Any, db: Database) -> None:
        pipe = pipeline_factory(stub_source("gh", [good_raw()]))

        summary = await pipe.run(dry_run=True)

        assert summary.accepted == 1
        assert summary.new_ids  # reported as if new
        assert await db.count_jobs() == 0
        assert await db.count_events("new", 1) == 0

    async def test_low_score_is_filtered_out(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        # raise the bar so the 68.5 vacancy is scored but then rejected
        summary = await pipeline_factory(
            stub_source("gh", [good_raw()]), min_score=90.0
        ).run()

        assert summary.accepted == 0
        assert summary.filtered == 1
        assert summary.new_ids == []
        assert await db.count_jobs() == 0


class TestAlerts:
    async def test_alert_only_at_or_above_threshold(
        self, pipeline_factory: Any
    ) -> None:
        quiet = await pipeline_factory(stub_source("gh", [good_raw()])).run()
        loud = await pipeline_factory(
            stub_source("gh", [good_raw()]), alert_score=60.0
        ).run()

        assert quiet.alerts == []
        assert loud.alerts == quiet.new_ids

    async def test_rich_vacancy_scores_higher(self, pipeline_factory: Any) -> None:
        summary = await pipeline_factory(
            stub_source("gh", [good_raw(description=RICH_DESC)])
        ).run()

        assert summary.accepted == 1
        assert summary.new_ids  # 82.0 > min_score
        assert summary.alerts == []  # still below the default 85


class TestFilterFlagsReachTheCard:
    async def test_geo_note_survives_scoring(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        # ``score_job`` rebuilds ``job.flags``; the filter note must be merged back in.
        pipe = pipeline_factory(
            stub_source(
                "gh",
                [good_raw(description=f"{GOOD_DESC} Must be located in Germany.")],
            )
        )
        summary = await pipe.run()

        row = await db.get_job(summary.new_ids[0])
        flags = json.loads(row["flags"])
        assert "гео-ограничение" in flags
        assert row["geo_restricted"] == 1

    async def test_scam_flag_is_set_by_the_scorer(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        summary = await pipeline_factory(
            stub_source("gh", [good_raw(description=f"{GOOD_DESC} Unpaid training fee.")])
        ).run()

        row = await db.get_job(summary.new_ids[0])
        assert row["suspicious"] == 1
        assert "suspicious" in json.loads(row["flags"])


class TestLearnedState:
    async def test_blocklist_round_trip(self, pipeline_factory: Any, db: Database) -> None:
        pipe = pipeline_factory()

        assert await pipe.add_block_company("Blocky") == ["Blocky"]
        assert await pipe.add_block_company("Blocky") == ["Blocky"]  # idempotent
        assert json.loads(await db.kv_get(KV_BLOCKLIST)) == ["Blocky"]

        assert await pipe.remove_block_company("Blocky") == []
        assert await pipe.remove_block_company("Blocky") == []

    async def test_blocklist_filters_the_run(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        pipe = pipeline_factory(stub_source("gh", [good_raw()]))
        await pipe.add_block_company("CryptoCo")

        summary = await pipe.run()

        assert summary.accepted == 0
        assert summary.filtered == 1

    async def test_garbage_in_kv_degrades_to_empty(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        pipe = pipeline_factory()

        await db.kv_set(KV_BLOCKLIST, "not json")
        assert await pipe._blocklist() == []

        await db.kv_set(KV_BLOCKLIST, '{"a": 1}')
        assert await pipe._blocklist() == []

    async def test_stopwords_learned_from_rejects(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        await db.kv_set("stopwords", json.dumps(["solidity"], ensure_ascii=False))
        pipe = pipeline_factory(
            stub_source("gh", [good_raw(title="Senior DevOps Engineer (Solidity)")])
        )

        summary = await pipe.run()

        assert summary.accepted == 0
        assert summary.filtered == 1


class TestChannelWiring:
    """``/addchannel`` lives in kv; the pipeline hands it to the TG plugin (ТЗ §7)."""

    async def test_channels_from_kv_extend_the_plugin(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        await db.kv_set(KV_CHANNELS, json.dumps(["crypto_jobs"], ensure_ascii=False))
        tg = TelegramSource(
            SourceConfig(name="tg", kind="telegram", queries=["devops_vacancies"]),
            make_fetcher(lambda request: httpx.Response(200, text="<html/>")),
        )
        pipe = pipeline_factory(tg)

        await pipe.fetch_all()  # the wiring happens inside fetch_all

        assert tg.channels == ["devops_vacancies", "crypto_jobs"]

    async def test_garbage_in_channels_kv_keeps_the_config(
        self, pipeline_factory: Any, db: Database
    ) -> None:
        await db.kv_set(KV_CHANNELS, "not json")
        tg = TelegramSource(
            SourceConfig(name="tg", kind="telegram", queries=["devops_vacancies"]),
            make_fetcher(lambda request: None),
        )

        await pipeline_factory(tg)._apply_channels()

        assert tg.channels == ["devops_vacancies"]

    async def test_pipeline_without_a_telegram_plugin_is_untouched(
        self, pipeline_factory: Any
    ) -> None:
        await pipeline_factory(stub_source("gh"))._apply_channels()


class TestLlmStep:
    async def test_llm_runs_when_enabled(self, pipeline_factory: Any) -> None:
        llm = CountingLLM()
        pipe = pipeline_factory(stub_source("gh", [good_raw()]), llm=llm)

        summary = await pipe.run()

        assert summary.llm_calls == 1
        assert summary.new_ids and llm.seen == summary.new_ids

    async def test_use_llm_false_skips_the_call(self, pipeline_factory: Any) -> None:
        llm = CountingLLM()
        pipe = pipeline_factory(stub_source("gh", [good_raw()]), llm=llm)

        summary = await pipe.run(use_llm=False)

        assert summary.llm_calls == 0
        assert llm.seen == []

    async def test_disabled_llm_is_skipped(self, pipeline_factory: Any) -> None:
        pipe = pipeline_factory(stub_source("gh", [good_raw()]), llm=StubLLM())

        summary = await pipe.run()

        assert summary.llm_calls == 0

    async def test_llm_none_result_is_not_counted(self, pipeline_factory: Any) -> None:
        llm = CountingLLM(result=None)
        pipe = pipeline_factory(stub_source("gh", [good_raw()]), llm=llm)

        summary = await pipe.run()

        assert summary.llm_calls == 0
        assert llm.seen  # it was asked, the model simply had nothing to say

    async def test_verdict_survives_the_later_upsert(
        self, settings: Any, db: Database, profile: Profile
    ) -> None:
        # Re-scoring happens before the row exists, so writing only through
        # ``set_llm`` matched zero rows and the verdict never reached the card.
        conf = settings.model_copy(update={"gemini_api_key": "secret"})
        pipe = Pipeline(
            conf,
            profile,
            db,
            http=make_fetcher(lambda request: httpx.Response(200, json={})),
            llm=LLMService(conf, db, client=RecordingLLMClient()),
            sources=[stub_source("gh", [good_raw()])],
        )

        summary = await pipe.run()

        assert summary.llm_calls == 1
        rows = await db.list_jobs()
        assert [(r["llm_verdict"], r["llm_summary"]) for r in rows] == [
            ("fit", "Отличный мэтч.")
        ]


class TestClose:
    async def test_closes_http_and_llm_clients(self, pipeline_factory: Any) -> None:
        client = ClosingClient()
        pipe = pipeline_factory()
        pipe.llm._client = client  # type: ignore[attr-defined]

        await pipe.close()

        assert client.closed is True


class TestRunSummaryText:
    def test_ok_sources_counts_only_successes(self) -> None:
        summary = RunSummary(
            sources=[SourceResult(name="a", ok=True), SourceResult(name="b", ok=False)]
        )

        assert summary.ok_sources == 1

    def test_as_text_without_llm(self) -> None:
        summary = RunSummary(
            sources=[SourceResult(name="a", ok=True), SourceResult(name="b", ok=False)],
            raw_total=3,
            duplicates=1,
            filtered=1,
            accepted=1,
            new_ids=["x"],
        )

        assert summary.as_text() == (
            "источников 1/2, получено 3, дублей 1, отсеяно 1, принято 1, новых 1, 0.0s"
        )

    def test_as_text_mentions_llm_calls(self) -> None:
        summary = RunSummary(llm_calls=2, accepted=1, new_ids=["x", "y"])

        assert summary.as_text() == (
            "источников 0/0, получено 0, дублей 0, отсеяно 0, принято 1, "
            "новых 2, LLM 2, 0.0s"
        )
