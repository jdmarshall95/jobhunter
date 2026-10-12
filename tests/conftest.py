"""Shared fixtures: profile, temp database, source stubs and an aiogram harness.

The aiogram harness is deliberately thin: a :class:`FakeSession` records every
outgoing Telegram method, and updates are pushed through
``Dispatcher.feed_update`` so the real router filters / FSM are exercised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.types import Chat, Message, Update
from aiogram.types.message import ContentType

from app.config import Profile, Settings, SourceConfig
from app.http import HttpFetcher
from app.models import Job, RawJob
from app.sources.base import SourcePlugin
from app.storage import Database

OWNER = 267655032


# --------------------------------------------------------------------- config
@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    """Settings that ignore the developer's real ``.env``."""
    return Settings(
        telegram_enabled=True,
        telegram_bot_token="1234:TEST",
        telegram_chat_id=str(OWNER),
        gemini_api_key="",
        db_path=str(tmp_path / "jobhunter.db"),
        backup_dir=str(tmp_path / "backups"),
        domain_min_interval=0.0,
        http_max_retries=1,
        fetch_concurrency=2,
    )


@pytest.fixture
def profile() -> Profile:
    return Profile(
        candidate={
            "stack_core": ["kubernetes", "terraform", "ansible", "gitlab ci"],
            "stack_plus": ["helm", "prometheus", "grafana", "aws"],
            "domains_priority": ["crypto", "fintech", "web3"],
            "facts": ["4 года в DevOps, из них 2 в крипте"],
        },
        filters={
            "include_keywords": ["devops", "sre", "platform engineer"],
            "exclude_keywords": ["junior", "intern"],
            "exclude_companies": ["Acme"],
        },
        min_score=10,
        alert_score=85,
    )


# ------------------------------------------------------------------ factories
def make_raw(**kwargs: Any) -> RawJob:
    base: dict[str, Any] = {
        "source": "gh",
        "company": "CryptoCo",
        "title": "Senior DevOps Engineer",
        "url": "https://a.io/jobs/1",
    }
    base.update(kwargs)
    return RawJob(**base)


def make_job(**kwargs: Any) -> Job:
    base: dict[str, Any] = {
        "source": "gh",
        "company": "CryptoCo",
        "title": "Senior DevOps Engineer",
        "url": "https://a.io/jobs/1",
    }
    base.update(kwargs)
    return Job(**base)


@pytest.fixture
def good_raw() -> RawJob:
    return make_raw(
        location="Remote (EMEA)",
        description=(
            "We are a <b>crypto</b> exchange looking for a Senior DevOps Engineer. "
            "Kubernetes, Terraform, Ansible and Prometheus. Russian-speaking team. "
            "$4,200 monthly, fully remote."
        ),
    )


# ------------------------------------------------------------------ database
@pytest.fixture
async def db(tmp_path: Any) -> Any:
    store = Database(tmp_path / "test.db")
    await store.connect()
    yield store
    await store.close()


async def seed(db: Database, **kwargs: Any) -> str:
    """Persist one job and return its content id."""
    defaults: dict[str, Any] = {
        "source": "gh",
        "company": "CryptoCo",
        "title": "Senior DevOps Engineer",
        "url": "https://a.io/jobs/1",
        "location": "Remote (EMEA)",
        "remote": True,
        "salary_min": 4000.0,
        "salary_max": 4500.0,
        "currency": "USD",
        "description": "Kubernetes and Terraform in a crypto company.",
        "score": 70.0,
        "sources": ["gh"],
        "first_seen_at": datetime.now(tz=UTC),
    }
    defaults.update(kwargs)
    job = Job(**defaults)
    await db.upsert_job(job)
    return job.id


# --------------------------------------------------------------- http helpers
def make_fetcher(handler: Any) -> HttpFetcher:
    import httpx

    return HttpFetcher(
        user_agent="jobhunter-tests/1.0",
        timeout=5.0,
        max_retries=1,
        domain_min_interval=0.0,
        transport=httpx.MockTransport(handler),
    )


class StubSource(SourcePlugin):
    """A source plugin that returns canned data (or raises)."""

    kind = "stub"

    def __init__(
        self,
        cfg: SourceConfig,
        http: HttpFetcher,
        jobs: list[RawJob] | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(cfg, http)
        self._jobs = jobs or []
        self._error = error

    async def fetch(self) -> list[RawJob]:
        if self._error is not None:
            raise self._error
        return list(self._jobs)


def stub_source(name: str, jobs: list[RawJob] | None = None, error: Exception | None = None):
    cfg = SourceConfig(name=name, kind="greenhouse", token=name)
    return StubSource(cfg, make_fetcher(lambda request: None), jobs, error)


# ------------------------------------------------------------- aiogram harness
class FakeSession(BaseSession):
    """Records every Telegram method the bot tries to call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []

    async def close(self) -> None:
        pass

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = False,
    ) -> Any:  # pragma: no cover - documents are never downloaded in tests
        return None, None

    async def make_request(
        self, bot: Bot, method: Any, timeout: int | None = None
    ) -> Any:  # pragma: no cover - trivial
        self.calls.append(method)
        name = type(method).__name__
        if name == "DeleteMessage":
            return True
        chat_id = getattr(method, "chat_id", OWNER)
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            chat_id = OWNER
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        return Message(
            message_id=len(self.calls),
            date=datetime.now(tz=UTC),
            chat=Chat(id=chat_id, type="private"),
            text=text,
            content_type=ContentType.TEXT,
        )


class StubSender:
    """Stands in for :class:`app.bot.sender.Sender` (no queue, no pacing)."""

    def __init__(self) -> None:
        self.items: list[tuple[int, str, Any]] = []

    def send(self, chat_id: int, text: str, *, reply_markup: Any = None, **_kw: Any) -> None:
        self.items.append((chat_id, text, reply_markup))

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.items]


class StubLLM:
    def __init__(self, draft: str | None = None) -> None:
        self._draft = draft
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return False

    async def rescore(self, job: Job, profile: Profile) -> Any:
        return None

    async def draft(self, job: Job, profile: Profile, lang: str = "ru") -> str | None:
        self.calls += 1
        return self._draft


class StubPipeline:
    def __init__(self, summary: Any = None, llm: StubLLM | None = None) -> None:
        self.llm = llm or StubLLM()
        self._summary = summary
        self.blocked: list[str] = []
        self.unblocked: list[str] = []
        self.run_calls = 0

    async def run(self, *, dry_run: bool = False, use_llm: bool | None = None) -> Any:
        self.run_calls += 1
        return self._summary

    async def add_block_company(self, company: str) -> list[str]:
        self.blocked.append(company)
        return self.blocked

    async def remove_block_company(self, company: str) -> list[str]:
        self.unblocked.append(company)
        return self.unblocked


@dataclass
class BotHarness:
    bot: Bot
    session: FakeSession
    dispatcher: Dispatcher
    ctx: Any
    _counter: int = field(default=0, init=False)

    @property
    def sent(self) -> list[Any]:
        return [m for m in self.session.calls if type(m).__name__ == "SendMessage"]

    @property
    def edited(self) -> list[Any]:
        return [m for m in self.session.calls if type(m).__name__ == "EditMessageText"]

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]

    @property
    def answers(self) -> list[Any]:
        return [m for m in self.session.calls if type(m).__name__ == "AnswerCallbackQuery"]

    def clear(self) -> None:
        self.session.calls.clear()

    async def command(self, text: str, *, chat_id: int = OWNER) -> None:
        self._counter += 1
        update = Update.model_validate(
            {
                "update_id": self._counter,
                "message": {
                    "message_id": self._counter,
                    "date": 1767225600,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id, "is_bot": False, "first_name": "Ivan"},
                    "text": text,
                },
            },
            context={"bot": self.bot},
        )
        await self.dispatcher.feed_update(self.bot, update)

    async def callback(self, data: str, *, chat_id: int = OWNER, message_id: int = 1) -> None:
        self._counter += 1
        update = Update.model_validate(
            {
                "update_id": self._counter,
                "callback_query": {
                    "id": f"cb{self._counter}",
                    "from": {"id": chat_id, "is_bot": False, "first_name": "Ivan"},
                    "chat_instance": "1",
                    "data": data,
                    "message": {
                        "message_id": message_id,
                        "date": 1767225600,
                        "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": chat_id, "is_bot": False, "first_name": "Ivan"},
                        "text": "card",
                    },
                },
            },
            context={"bot": self.bot},
        )
        await self.dispatcher.feed_update(self.bot, update)


def build_harness(
    *,
    db: Database,
    profile: Profile,
    settings: Settings,
    pipeline: Any | None = None,
    owners: set[int] | None = None,
) -> BotHarness:
    from app.bot.handlers import BotCtx, build_router

    session = FakeSession()
    # same defaults as production (``App.start_bot``): HTML everywhere
    bot = Bot(
        token="1234:TEST",
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    ctx = BotCtx(
        settings=settings,
        profile=profile,
        db=db,
        pipeline=pipeline or StubPipeline(),
        sender=StubSender(),
        owners=owners if owners is not None else {OWNER},
        api=bot,
    )
    dispatcher = Dispatcher(storage=None)
    dispatcher.include_router(build_router(ctx))
    return BotHarness(bot=bot, session=session, dispatcher=dispatcher, ctx=ctx)


@pytest.fixture
def harness_factory(db: Database, profile: Profile, settings: Settings) -> Any:
    def _factory(**kwargs: Any) -> BotHarness:
        return build_harness(db=db, profile=profile, settings=settings, **kwargs)

    return _factory
