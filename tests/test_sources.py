"""Source plugins: pure parsers plus ``fetch()`` happy/failure paths (ТЗ §5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.config import SourceConfig, SourcesConfig
from app.sources import REGISTRY, build_source, build_sources
from app.sources.ashby import parse_ashby
from app.sources.base import SourceError
from app.sources.greenhouse import parse_greenhouse
from app.sources.habr import parse_habr, parse_salary_rub
from app.sources.hh import parse_hh
from app.sources.lever import parse_lever
from app.sources.remoteok import parse_remoteok
from app.sources.telegram import DEFAULT_CHANNELS, TelegramSource, parse_telegram
from app.sources.wwr_rss import parse_wwr
from tests.conftest import make_fetcher

# --------------------------------------------------------------------- payloads

GH_PAYLOAD: dict[str, Any] = {
    "jobs": [
        {
            "title": "Senior DevOps Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
            "location": {"name": "Remote — EMEA"},
            "content": "<p>Kubernetes, Terraform</p>",
            "updated_at": "2026-10-01T10:00:00Z",
            "id": 123,
            "type": "Full-Time",
        },
        {
            "title": "SRE",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/124",
            "location": "Berlin",
            "description": "plain text fallback",
            "first_published": "2026-09-20T08:00:00Z",
            "id": 124,
        },
        {"title": "  ", "absolute_url": "https://boards.greenhouse.io/acme/jobs/125"},
        {"title": "No URL"},
    ]
}

ASHBY_PAYLOAD: dict[str, Any] = {
    "jobs": [
        {
            "title": "Platform Engineer",
            "jobUrl": "https://jobs.ashbyhq.com/acme/1",
            "location": {"location": "Remote, EMEA"},
            "techStack": ["kubernetes", "terraform"],
            "descriptionHtml": "<p>infra</p>",
            "isRemote": True,
            "publishedAt": "2026-10-02T00:00:00Z",
            "id": "a1",
            "department": "Platform",
        },
        {
            "title": "Site Reliability Engineer",
            "applyUrl": "https://jobs.ashbyhq.com/acme/2",
            "location": "Lisbon",
            "descriptionPlain": "plain",
            "id": "a2",
        },
    ]
}

LEVER_PAYLOAD: list[dict[str, Any]] = [
    {
        "text": "DevOps Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/1",
        "categories": {"location": "Remote", "workplaceType": "remote", "team": "Infra"},
        "lists": {"descriptionPlain": "plain desc"},
        "createdAt": 1759276800000,
        "id": "l1",
    },
    {
        "text": "Senior SRE",
        "applyUrl": "https://jobs.lever.co/acme/2",
        "categories": {"workplaceType": "office"},
        "description": "top-level desc",
        "id": "l2",
    },
    {"text": "Hybrid Guy", "hostedUrl": "https://jobs.lever.co/acme/3", "id": "l3"},
    {"hostedUrl": "https://jobs.lever.co/acme/4"},
]

REMOTEOK_PAYLOAD: list[Any] = [
    "2026-10-05T09:00:00+00:00",  # element 0 is a header comment, not a job
    {
        "position": "DevOps Engineer",
        "url": "https://remoteok.com/remote-jobs/1",
        "company": "CryptoCo",
        "location": None,
        "salary_min": 60000,
        "salary_max": 90000,
        "salary_currency": "USD",
        "tags": ["devops", "kubernetes", "web3"],
        "category": "devops",
        "id": 1,
    },
    {
        "position": "SRE",
        "url": "https://remoteok.com/remote-jobs/2",
        "location": "Germany",
        "description": "desc here",
        "id": 2,
    },
    {"url": "https://remoteok.com/remote-jobs/3"},  # no position -> skipped
]

HH_PAYLOAD: dict[str, Any] = {
    "found": 2,
    "items": [
        {
            "name": "DevOps-инженер",
            "alternate_url": "https://hh.ru/vacancy/1",
            "area": {"id": "1", "name": "Москва"},
            "schedule": {"name": "Удаленная работа"},
            "employer": {"name": "Тинькофф"},
            "snippet": {"requirement": "Опыт 3 года", "responsibility": "Поддержка кластеров"},
            "professional_roles": [{"name": "DevOps"}, {"name": "SRE"}],
            "department": "Платформа",
            "published_at": "2026-10-01T07:00:00Z",
            "id": "1",
        },
        {
            "name": "SRE",
            "alternate_url": "https://hh.ru/vacancy/2",
            "area": {"name": "Санкт-Петербург"},
            "schedule": {"name": "Гибрид"},
            "employer": {},
            "id": "2",
        },
    ],
}

WWR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>WWR programming</title><link>https://weworkremotely.com</link>
<description>feed</description>
<item>
  <title>Chainalysis: Senior DevOps Engineer</title>
  <link>https://weworkremotely.com/remote-jobs/chainalysis-devops</link>
  <description>Kubernetes and Terraform</description>
  <guid>wwr-1</guid>
  <pubDate>Tue, 30 Sep 2026 10:00:00 +0000</pubDate>
</item>
<item>
  <title>Acme: Product Designer</title>
  <link>https://weworkremotely.com/remote-jobs/acme-designer</link>
  <description>Figma</description>
  <guid>wwr-2</guid>
</item>
</channel></rss>
"""

# Реальный формат объявления Хабр Карьеры (проверено на живом RSS): роль в «»,
# компания в <author>, зарплата по-русски, город — первым предложением описания.
HABR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Хабр Карьера: вакансии</title><link>https://career.habr.com/vacancies</link>
<description>vacancies</description>
<item>
  <title>Требуется «Системный администратор» (Ярославль, от 100 000 ₽)</title>
  <link>https://career.habr.com/vacancies/2400001</link>
  <author>Raft</author>
  <description>Компания «Raft» ищет специалиста на вакансию «Системный администратор».
  Ярославль (Россия). От 100 000 ₽. Можно удалённо.
  Требуемые навыки: #middle, #Linux, #Zabbix.</description>
  <guid>habr-1</guid>
  <pubDate>Wed, 01 Oct 2026 09:30:00 +0300</pubDate>
</item>
<item>
  <title>Требуется «Руководитель проекта IT (B2G)» (Торжок)</title>
  <link>https://career.habr.com/vacancies/2400002</link>
  <author>НПО Реестр</author>
  <description>Компания «НПО Реестр» ищет специалиста на вакансию
  «Руководитель проекта IT (B2G)». Торжок (Россия). Полный рабочий день.
  Требуемые навыки: #middle.</description>
  <guid>habr-2</guid>
</item>
<item>
  <title>Требуется «DevOps-инженер» (Москва, от 300 000 ₽ до 500 000 ₽)</title>
  <link>https://career.habr.com/vacancies/2400003</link>
  <author>КриптоБанк</author>
  <description>Компания «КриптоБанк» ищет специалиста на вакансию «DevOps-инженер».
  Москва (Россия). От 300 000 ₽ до 500 000 ₽. Полный рабочий день.
  Требуемые навыки: #Kubernetes, #Terraform.</description>
  <guid>habr-3</guid>
</item>
<item>
  <title>Ищем SRE (Москва)</title>
  <link>https://career.habr.com/vacancies/2400004</link>
  <description>Компания «Техно» ищет специалиста на вакансию «SRE».
  Москва (Россия). До 180 000 ₽.
  Требуемые навыки: #senior.</description>
  <guid>habr-4</guid>
</item>
</channel></rss>
"""

# Публичное превью t.me/s/<channel> (проверено на devops_vacancies).
TG_HTML = """<html><body>
<div class="tgme_widget_message" data-post="devops_vacancies/42">
  <a class="tgme_widget_message_owner_name"><span>DevOps вакансии | работа</span></a>
  <div class="tgme_widget_message_text js-message_text">🔥 <b>Системный инженер/DevOps Engineer</b>
  <br>Москва<br>от 200 000 до 300 000 ₽<br>ссылка</div>
  <time datetime="2026-10-01T03:08:05+00:00">1 октября</time>
</div>
<div class="tgme_widget_message" data-post="devops_vacancies/43">
  <a class="tgme_widget_message_owner_name"><span>DevOps вакансии | работа</span></a>
  <div class="tgme_widget_message_text js-message_text">Product Manager<br>ссылка</div>
  <time datetime="2026-10-02T03:08:05+00:00">2 октября</time>
</div>
<div class="tgme_widget_message" data-post="devops_vacancies/44">
  <time datetime="2026-10-03T03:08:05+00:00">3 октября</time>
</div>
</body></html>
"""


# --------------------------------------------------------------------- parsers


class TestGreenhouse:
    def test_parses_jobs_and_skips_incomplete(self) -> None:
        jobs = parse_greenhouse(GH_PAYLOAD, company="Acme", source="gh")

        assert [j.title for j in jobs] == ["Senior DevOps Engineer", "SRE"]
        assert all(j.company == "Acme" for j in jobs)
        assert all(j.source == "gh" for j in jobs)

    def test_location_accepts_dict_and_string(self) -> None:
        jobs = parse_greenhouse(GH_PAYLOAD, company="Acme", source="gh")

        assert jobs[0].location == "Remote — EMEA"
        assert jobs[1].location == "Berlin"

    def test_description_and_posted_at_fallbacks(self) -> None:
        jobs = parse_greenhouse(GH_PAYLOAD, company="Acme", source="gh")

        assert jobs[0].description == "<p>Kubernetes, Terraform</p>"
        assert jobs[1].description == "plain text fallback"
        assert jobs[0].posted_at == "2026-10-01T10:00:00Z"
        assert jobs[1].posted_at == "2026-09-20T08:00:00Z"
        assert jobs[0].raw == {"id": 123, "type": "Full-Time"}

    def test_empty_payload(self) -> None:
        assert parse_greenhouse({}, company="Acme", source="gh") == []


class TestAshby:
    def test_tech_stack_is_prefixed_to_description(self) -> None:
        jobs = parse_ashby(ASHBY_PAYLOAD, company="Acme", source="ashby")

        assert jobs[0].description == "Stack: kubernetes, terraform\n<p>infra</p>"
        assert jobs[1].description == "plain"

    def test_location_dict_or_string(self) -> None:
        jobs = parse_ashby(ASHBY_PAYLOAD, company="Acme", source="ashby")

        assert jobs[0].location == "Remote, EMEA"
        assert jobs[1].location == "Lisbon"

    def test_remote_is_tri_state(self) -> None:
        jobs = parse_ashby(ASHBY_PAYLOAD, company="Acme", source="ashby")

        assert jobs[0].remote is True
        assert jobs[1].remote is None  # "isRemote" absent -> unknown

    def test_company_from_item_wins(self) -> None:
        payload = {"jobs": [{"title": "X", "jobUrl": "https://a/1", "company": "RealCo"}]}

        assert parse_ashby(payload, company="Acme", source="ashby")[0].company == "RealCo"


class TestLever:
    def test_remote_flag_mapping(self) -> None:
        jobs = parse_lever(LEVER_PAYLOAD, company="Acme", source="lever")

        assert jobs[0].remote is True  # workplaceType "remote"
        assert jobs[1].remote is False  # "office"
        assert jobs[2].remote is None  # absent

    def test_url_and_description_fallbacks(self) -> None:
        jobs = parse_lever(LEVER_PAYLOAD, company="Acme", source="lever")

        assert jobs[0].url == "https://jobs.lever.co/acme/1"
        assert jobs[1].url == "https://jobs.lever.co/acme/2"  # applyUrl
        assert jobs[0].description == "plain desc"
        assert jobs[1].description == "top-level desc"

    def test_skips_items_without_title_or_url(self) -> None:
        assert len(parse_lever(LEVER_PAYLOAD, company="Acme", source="lever")) == 3


class TestRemoteOK:
    def test_skips_header_comment_element(self) -> None:
        jobs = parse_remoteok(REMOTEOK_PAYLOAD, source="remoteok")

        assert [j.title for j in jobs] == ["DevOps Engineer", "SRE"]

    def test_salary_and_currency(self) -> None:
        jobs = parse_remoteok(REMOTEOK_PAYLOAD, source="remoteok")

        assert (jobs[0].salary_min, jobs[0].salary_max) == (60000.0, 90000.0)
        assert jobs[0].currency == "USD"
        assert (jobs[1].salary_min, jobs[1].salary_max) == (None, None)
        assert jobs[1].currency == "USD"  # default when the field is absent

    def test_missing_location_means_remote(self) -> None:
        jobs = parse_remoteok(REMOTEOK_PAYLOAD, source="remoteok")

        assert jobs[0].remote is True  # location None
        assert jobs[1].remote is None  # "Germany" -> not confirmed remote

    def test_description_falls_back_to_tags(self) -> None:
        jobs = parse_remoteok(REMOTEOK_PAYLOAD, source="remoteok")

        assert jobs[0].description == "Tags: devops, kubernetes, web3"
        assert jobs[1].description == "desc here"


class TestHH:
    def test_remote_only_from_schedule(self) -> None:
        jobs = parse_hh(HH_PAYLOAD, source="hh")

        assert jobs[0].remote is True  # "Удаленная работа"
        assert jobs[1].remote is None  # "Гибрид" is not confirmed remote

    def test_description_is_snippet(self) -> None:
        jobs = parse_hh(HH_PAYLOAD, source="hh")

        assert jobs[0].description == "Опыт 3 года\nПоддержка кластеров"

    def test_employer_fallback_and_roles(self) -> None:
        jobs = parse_hh(HH_PAYLOAD, source="hh")

        assert jobs[0].company == "Тинькофф"
        assert jobs[1].company == "hh.ru"  # empty employer object
        assert jobs[0].raw["roles"] == "DevOps, SRE"
        assert jobs[0].raw["area"] == "1"


class TestWWR:
    def test_company_from_title_prefix(self) -> None:
        jobs = parse_wwr({"entries": _entries()}, source="wwr")

        assert jobs[0].company == "Chainalysis"

    def test_queries_prefilter(self) -> None:
        entries = _entries()

        filtered = parse_wwr({"entries": entries}, source="wwr", queries=["devops"])
        assert [j.title for j in filtered] == ["Chainalysis: Senior DevOps Engineer"]

        unfiltered = parse_wwr({"entries": entries}, source="wwr")
        assert len(unfiltered) == 2

    def test_always_marks_remote(self) -> None:
        jobs = parse_wwr({"entries": _entries()}, source="wwr")

        assert jobs[0].location == "Remote"
        assert jobs[0].remote is True


def _entries() -> list[dict[str, Any]]:
    import feedparser

    return list(feedparser.parse(WWR_RSS)["entries"])


def _habr_entries() -> list[dict[str, Any]]:
    import feedparser

    return list(feedparser.parse(HABR_RSS)["entries"])


class TestHabr:
    def test_role_company_and_location(self) -> None:
        jobs = parse_habr({"entries": _habr_entries()}, source="habr")

        assert [j.title for j in jobs] == [
            "Системный администратор",
            "Руководитель проекта IT (B2G)",
            "DevOps-инженер",
            "SRE",
        ]
        assert [j.company for j in jobs] == ["Raft", "НПО Реестр", "КриптоБанк", "Техно"]
        assert jobs[0].location == "Ярославль (Россия)"
        assert jobs[2].location == "Москва (Россия)"

    def test_russian_salary_is_extracted_as_rub(self) -> None:
        # Общий parse_salary такую форму не видит — плагин отдаёт явный диапазон.
        jobs = parse_habr({"entries": _habr_entries()}, source="habr")

        assert (jobs[0].salary_min, jobs[0].salary_max) == (100_000.0, None)
        assert (jobs[2].salary_min, jobs[2].salary_max) == (300_000.0, 500_000.0)
        assert (jobs[3].salary_min, jobs[3].salary_max) == (None, 180_000.0)
        assert jobs[2].currency == "RUB"
        assert jobs[1].currency == ""

    def test_remote_is_true_only_when_the_feed_says_so(self) -> None:
        jobs = parse_habr({"entries": _habr_entries()}, source="habr")

        assert jobs[0].remote is True  # «Можно удалённо» — с ё
        assert jobs[2].remote is None  # офис: жёсткий фильтр не должен отсекать

    def test_queries_prefilter(self) -> None:
        jobs = parse_habr({"entries": _habr_entries()}, source="habr", queries=["devops"])

        assert [j.title for j in jobs] == ["DevOps-инженер"]

    def test_posted_at_is_converted_from_rfc822(self) -> None:
        jobs = parse_habr({"entries": _habr_entries()}, source="habr")

        assert jobs[0].posted_at == datetime(2026, 10, 1, 6, 30, tzinfo=UTC)
        assert jobs[1].posted_at is None

    def test_skills_drop_the_trailing_sentence_dot(self) -> None:
        jobs = parse_habr({"entries": _habr_entries()}, source="habr")

        assert jobs[0].raw["skills"] == ["middle", "Linux", "Zabbix"]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("От 300 000 ₽ до 500 000 ₽", (300_000.0, 500_000.0)),
            ("Вилка: от 220 000 ₽ до 240 000 ₽", (220_000.0, 240_000.0)),
            ("(Торжок, от 100 000 ₽)", (100_000.0, None)),
            ("(до 180 000 ₽)", (None, 180_000.0)),
            ("от 3 000 USD", (None, None)),
            ("навыки: #middle", (None, None)),
            ("", (None, None)),
        ],
    )
    def test_parse_salary_rub(self, text: str, expected: Any) -> None:
        assert parse_salary_rub(text) == expected


class TestTelegram:
    def test_title_is_the_first_meaningful_line(self) -> None:
        jobs = parse_telegram(TG_HTML, channel="devops_vacancies", source="tg")

        assert [j.title for j in jobs] == ["Системный инженер/DevOps Engineer", "Product Manager"]

    def test_url_company_and_posted_at(self) -> None:
        jobs = parse_telegram(TG_HTML, channel="devops_vacancies", source="tg")

        assert jobs[0].url == "https://t.me/devops_vacancies/42"
        assert jobs[0].company == "DevOps вакансии | работа"
        assert jobs[0].posted_at == "2026-10-01T03:08:05+00:00"
        assert jobs[0].raw == {"post": "devops_vacancies/42", "channel": "devops_vacancies"}

    def test_messages_without_text_are_skipped(self) -> None:
        assert len(parse_telegram(TG_HTML, channel="devops_vacancies", source="tg")) == 2

    def test_limit_caps_the_channel(self) -> None:
        jobs = parse_telegram(TG_HTML, channel="devops_vacancies", source="tg", limit=1)

        assert [j.title for j in jobs] == ["Системный инженер/DevOps Engineer"]

    def test_queries_prefilter(self) -> None:
        jobs = parse_telegram(
            TG_HTML, channel="devops_vacancies", source="tg", queries=["devops"]
        )

        assert [j.title for j in jobs] == ["Системный инженер/DevOps Engineer"]

    def test_url_and_company_fall_back_to_the_channel(self) -> None:
        html = (
            '<div class="tgme_widget_message">'
            '<div class="tgme_widget_message_text">DevOps</div>'
            "</div>"
        )

        jobs = parse_telegram(html, channel="getjob", source="tg")

        assert jobs[0].url == "https://t.me/s/getjob"
        assert jobs[0].company == "@getjob"

    def test_lunapark_tldr_title_and_company(self) -> None:
        html = (
            '<div class="tgme_widget_message" data-post="hrlunapark/408">'
            '<a class="tgme_widget_message_owner_name"><span>Take Friends to Luna Park</span></a>'
            '<div class="tgme_widget_message_text">'
            "tl;dr: founding full-stack engineer (early hire), $100k–130k + equity, remote\n"
            "\n"
            "Reelful — приложение где AI-агенты монтируют рилсы.\n"
            "Основательница — Катя Дейнека."
            "</div>"
            '<time datetime="2026-10-09T12:00:00+00:00">9 October</time>'
            "</div>"
        )

        jobs = parse_telegram(html, channel="hrlunapark", source="tg")

        assert len(jobs) == 1
        assert jobs[0].title.startswith("founding full-stack engineer")
        assert jobs[0].company == "Reelful"
        assert jobs[0].url == "https://t.me/hrlunapark/408"
        assert jobs[0].raw["channel"] == "hrlunapark"

    def test_lunapark_without_company_dash_uses_channel_handle(self) -> None:
        html = (
            '<div class="tgme_widget_message" data-post="hrlunapark/1">'
            '<a class="tgme_widget_message_owner_name"><span>Take Friends to Luna Park</span></a>'
            '<div class="tgme_widget_message_text">'
            "tl;dr: ml engineers, $130k-200k, London\n"
            "Короткий пост про найм без тире в названии."
            "</div></div>"
        )

        jobs = parse_telegram(html, channel="hrlunapark", source="tg")

        assert jobs[0].company == "@hrlunapark"
        assert "ml engineers" in jobs[0].title


class TestTelegramChannels:
    def test_config_and_bot_channels_are_merged(self) -> None:
        cfg = SourceConfig(name="unit", kind="telegram", queries=["devops_vacancies", "@getjob"])
        src = TelegramSource(cfg, make_fetcher(lambda request: None))

        src.add_channels(["@getjob", "crypto_jobs", ""])

        assert src.channels == ["devops_vacancies", "getjob", "crypto_jobs"]

    def test_empty_config_uses_the_verified_defaults(self) -> None:
        cfg = SourceConfig(name="unit", kind="telegram")

        assert TelegramSource(cfg, make_fetcher(lambda request: None)).channels == list(
            DEFAULT_CHANNELS
        )


# ------------------------------------------------------------------ fetch(): I/O


def _plugin(cls: Any, payload: Any, **cfg_kwargs: Any) -> Any:
    """Build a plugin whose HTTP client always answers with ``payload``."""
    cfg = SourceConfig(name="unit", kind=cls.kind, token="acme", **cfg_kwargs)
    return cls(cfg, make_fetcher(lambda request: httpx.Response(200, json=payload)))


class TestFetchHappyPath:
    async def test_greenhouse_uses_token_as_company(self) -> None:
        src = _plugin(REGISTRY["greenhouse"], GH_PAYLOAD)

        jobs = await src.fetch()

        assert len(jobs) == 2
        assert jobs[0].company == "acme"  # cfg.company absent -> token
        assert jobs[0].source == "unit"

    async def test_greenhouse_prefers_cfg_company(self) -> None:
        src = _plugin(REGISTRY["greenhouse"], GH_PAYLOAD, company="Acme Corp")

        assert (await src.fetch())[0].company == "Acme Corp"

    async def test_ashby(self) -> None:
        jobs = await _plugin(REGISTRY["ashby"], ASHBY_PAYLOAD).fetch()

        assert [j.title for j in jobs] == ["Platform Engineer", "Site Reliability Engineer"]

    async def test_lever(self) -> None:
        jobs = await _plugin(REGISTRY["lever"], LEVER_PAYLOAD).fetch()

        assert len(jobs) == 3

    async def test_remoteok(self) -> None:
        jobs = await _plugin(REGISTRY["remoteok"], REMOTEOK_PAYLOAD).fetch()

        assert [j.title for j in jobs] == ["DevOps Engineer", "SRE"]

    async def test_hh(self) -> None:
        jobs = await _plugin(REGISTRY["hh"], HH_PAYLOAD).fetch()

        assert len(jobs) == 2

    async def test_wwr(self) -> None:
        cfg = SourceConfig(name="unit", kind="wwr_rss", queries=["devops"])
        src = REGISTRY["wwr_rss"](
            cfg, make_fetcher(lambda request: httpx.Response(200, text=WWR_RSS))
        )

        jobs = await src.fetch()

        assert [j.company for j in jobs] == ["Chainalysis"]

    async def test_habr(self) -> None:
        cfg = SourceConfig(name="unit", kind="habr", queries=["devops"])
        src = REGISTRY["habr"](
            cfg, make_fetcher(lambda request: httpx.Response(200, text=HABR_RSS))
        )

        jobs = await src.fetch()

        assert [(j.company, j.salary_min) for j in jobs] == [("КриптоБанк", 300_000.0)]

    async def test_telegram_scrapes_every_configured_channel(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(404 if "dead" in str(request.url) else 200, text=TG_HTML)

        cfg = SourceConfig(
            name="unit", kind="telegram", queries=["devops_vacancies", "dead_channel"]
        )
        jobs = await REGISTRY["telegram"](cfg, make_fetcher(handler)).fetch()

        assert seen == ["https://t.me/s/devops_vacancies", "https://t.me/s/dead_channel"]
        assert len(jobs) == 2  # a dead channel does not void the healthy one
        assert {j.raw["channel"] for j in jobs} == {"devops_vacancies"}

    async def test_telegram_without_queries_uses_the_defaults(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, text=TG_HTML)

        cfg = SourceConfig(name="unit", kind="telegram")
        await REGISTRY["telegram"](cfg, make_fetcher(handler)).fetch()

        assert seen == [f"https://t.me/s/{name}" for name in DEFAULT_CHANNELS]


class TestFetchRequests:
    async def test_greenhouse_url_contains_token(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=GH_PAYLOAD)

        cfg = SourceConfig(name="unit", kind="greenhouse", token="blockdaemon")
        await REGISTRY["greenhouse"](cfg, make_fetcher(handler)).fetch()

        assert seen == ["https://boards-api.greenhouse.io/v1/boards/blockdaemon/jobs?content=true"]

    async def test_hh_sends_required_header_and_params(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=HH_PAYLOAD)

        cfg = SourceConfig(
            name="unit", kind="hh", queries=["devops", "sre"], note="jobhunter-test/1.0"
        )
        await REGISTRY["hh"](cfg, make_fetcher(handler)).fetch()

        request = seen[0]
        assert request.headers["HH-User-Agent"] == "jobhunter-test/1.0"
        assert "api.hh.ru/vacancies" in str(request.url)
        assert "per_page=50" in str(request.url)
        assert "search=devops+OR+sre" in str(request.url)

    async def test_hh_default_user_agent_when_note_empty(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=HH_PAYLOAD)

        cfg = SourceConfig(name="unit", kind="hh")
        await REGISTRY["hh"](cfg, make_fetcher(handler)).fetch()

        assert seen[0].headers["HH-User-Agent"].startswith("jobhunter/0.1")


class TestFetchFailures:
    async def test_no_data_raises_with_http_status(self) -> None:
        cfg = SourceConfig(name="unit", kind="greenhouse", token="acme")
        src = REGISTRY["greenhouse"](cfg, make_fetcher(lambda request: httpx.Response(403)))

        with pytest.raises(SourceError, match=r"unit: no data \(http 403\)"):
            await src.fetch()

    @pytest.mark.parametrize(
        ("kind", "payload"),
        [
            ("greenhouse", []),  # dict expected
            ("ashby", []),
            ("hh", []),
            ("lever", {"jobs": []}),  # list expected
            ("remoteok", {"a": 1}),
        ],
    )
    async def test_unexpected_payload_shape(self, kind: str, payload: Any) -> None:
        cfg = SourceConfig(name="unit", kind=kind, token="acme")
        src = REGISTRY[kind](cfg, make_fetcher(lambda request: httpx.Response(200, json=payload)))

        with pytest.raises(SourceError, match="unexpected payload shape"):
            await src.fetch()

    async def test_wwr_empty_feed_raises(self) -> None:
        cfg = SourceConfig(name="unit", kind="wwr_rss")
        src = REGISTRY["wwr_rss"](
            cfg, make_fetcher(lambda request: httpx.Response(200, text="<html/>"))
        )

        with pytest.raises(SourceError, match="feed has no entries"):
            await src.fetch()

    async def test_wwr_no_data_raises(self) -> None:
        # 4xx is not retried, so the status reaches the error message verbatim.
        cfg = SourceConfig(name="unit", kind="wwr_rss")
        src = REGISTRY["wwr_rss"](cfg, make_fetcher(lambda request: httpx.Response(404)))

        with pytest.raises(SourceError, match=r"unit: no data \(http 404\)"):
            await src.fetch()

    async def test_habr_empty_feed_raises(self) -> None:
        cfg = SourceConfig(name="unit", kind="habr")
        src = REGISTRY["habr"](
            cfg, make_fetcher(lambda request: httpx.Response(200, text="<html/>"))
        )

        with pytest.raises(SourceError, match="feed has no entries"):
            await src.fetch()

    async def test_habr_no_data_raises(self) -> None:
        cfg = SourceConfig(name="unit", kind="habr")
        src = REGISTRY["habr"](cfg, make_fetcher(lambda request: httpx.Response(401)))

        with pytest.raises(SourceError, match=r"unit: no data \(http 401\)"):
            await src.fetch()

    async def test_telegram_empty_preview_raises(self) -> None:
        cfg = SourceConfig(name="unit", kind="telegram", queries=["devops_ru"])
        src = REGISTRY["telegram"](
            cfg, make_fetcher(lambda request: httpx.Response(200, text="<html/>"))
        )

        with pytest.raises(SourceError, match=r"unit: telegram feed empty: no messages"):
            await src.fetch()

    async def test_telegram_all_channels_down_raises(self) -> None:
        cfg = SourceConfig(name="unit", kind="telegram", queries=["a", "b"])
        src = REGISTRY["telegram"](cfg, make_fetcher(lambda request: httpx.Response(404)))

        with pytest.raises(
            SourceError, match=r"telegram feed empty: @a \(http 404\), @b \(http 404\)"
        ):
            await src.fetch()

    async def test_exhausted_retries_report_status_zero(self) -> None:
        # 5xx is retried; when the retries run out the status is lost (0), so the
        # message carries no "(http N)" suffix at all.
        cfg = SourceConfig(name="unit", kind="wwr_rss")
        src = REGISTRY["wwr_rss"](cfg, make_fetcher(lambda request: httpx.Response(503)))

        with pytest.raises(SourceError, match="unit: no data$"):
            await src.fetch()


# ---------------------------------------------------------------------- registry


class TestRegistry:
    def test_registry_covers_all_documented_kinds(self) -> None:
        assert set(REGISTRY) == {
            "greenhouse",
            "ashby",
            "lever",
            "remoteok",
            "wwr_rss",
            "hh",
            "habr",
            "telegram",
        }

    @pytest.mark.parametrize(("kind", "name"), [("habr", "habr"), ("telegram", "tg")])
    def test_new_kinds_resolve_to_a_plugin(self, kind: str, name: str) -> None:
        plugin = build_source(
            SourceConfig(name=name, kind=kind), make_fetcher(lambda request: None)
        )

        assert plugin is not None
        assert plugin.kind == kind

    def test_build_source_resolves_kind(self) -> None:
        cfg = SourceConfig(name="gh", kind="greenhouse", token="acme")

        plugin = build_source(cfg, make_fetcher(lambda request: None))

        assert plugin is not None
        assert plugin.kind == "greenhouse"
        assert plugin.name == "gh"
        assert plugin.label == "acme"  # label falls back to token

    def test_unknown_kind_is_skipped_not_fatal(self) -> None:
        # LinkedIn is deliberately unsupported (ТЗ §5.7: no scraping), so the kind
        # is not in the registry; model_construct bypasses the Literal validation.
        cfg = SourceConfig.model_construct(name="li", kind="linkedin")

        assert build_source(cfg, make_fetcher(lambda request: None)) is None

    def test_build_sources_skips_disabled_and_unknown(self) -> None:
        cfg = SourcesConfig(
            sources=[
                SourceConfig(name="a", kind="greenhouse", token="a"),
                SourceConfig(name="b", kind="lever", token="b", enabled=False),
                SourceConfig.model_construct(name="c", kind="linkedin"),
                SourceConfig(name="d", kind="remoteok"),
            ]
        )

        plugins = build_sources(cfg, make_fetcher(lambda request: None))

        assert [p.name for p in plugins] == ["a", "d"]
