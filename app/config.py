"""Configuration: environment (.env) + YAML profile/sources.

Secrets are read from the environment only. Values are never logged; see
``app.logging_setup`` for the masking filter.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_ROOT = Path(__file__).resolve().parent.parent


class SalaryExpectation(BaseModel):
    """Compensation expectation.

    The customer quotes figures **net** («на руки»), so ``net: true`` keeps the
    semantics explicit: a vacancy offering the same number *gross* is worth less
    than it looks. ``tax_rate`` is an averaged flat tax used to convert between
    net and gross; annual figures are derived, never configured.
    """

    min: int = 3000
    target: int = 4000
    #: ``min``/``target`` are "на руки" (after tax).
    net: bool = True
    #: Averaged tax load used for the net <-> gross conversion (13%).
    tax_rate: float = 0.13

    @property
    def keep(self) -> float:
        """Share of a gross salary that survives taxes."""
        return max(0.05, 1.0 - self.tax_rate)

    def to_net(self, gross: float) -> float:
        return gross * self.keep if self.net else gross

    def to_gross(self, net: float) -> float:
        return net / self.keep if self.net else net

    @property
    def gross_min(self) -> float:
        return self.to_gross(float(self.min))

    @property
    def gross_target(self) -> float:
        return self.to_gross(float(self.target))

    @property
    def annual_net_min(self) -> float:
        return float(self.min) * 12

    @property
    def annual_net_target(self) -> float:
        return float(self.target) * 12

    @property
    def annual_gross_min(self) -> float:
        return self.gross_min * 12

    @property
    def annual_gross_target(self) -> float:
        return self.gross_target * 12

    def describe(self) -> str:
        """Human-readable band for ``/filters``: net, gross and annual."""
        net = f"${self.min:,}–${self.target:,} net"
        gross = f"≈${self.gross_min:,.0f}–${self.gross_target:,.0f} gross"
        annual = f"${self.annual_gross_min:,.0f}–${self.annual_gross_target:,.0f}/год gross"
        return f"{net} ({gross} · {annual}, налог {self.tax_rate:.0%})"


class Candidate(BaseModel):
    name: str = "Ivan"
    languages: list[str] = Field(default_factory=lambda: ["ru", "en"])
    target_roles: list[str] = Field(
        default_factory=lambda: [
            "DevOps",
            "SRE",
            "Platform Engineer",
            "Infrastructure Engineer",
            "MLOps",
        ]
    )
    seniority: list[str] = Field(default_factory=lambda: ["middle", "senior", "lead"])
    work_mode: Literal["remote", "hybrid", "onsite"] = "remote"
    salary_expectation_usd_month: SalaryExpectation = Field(default_factory=SalaryExpectation)
    stack_core: list[str] = Field(default_factory=list)
    stack_plus: list[str] = Field(default_factory=list)
    domains_priority: list[str] = Field(default_factory=list)
    want_russian_speaking_team: bool = True
    #: Verified achievements only — the LLM must not invent experience.
    facts: list[str] = Field(default_factory=list)
    headline: str = ""
    summary: str = ""


#: Breakdown/flag label used when a vacancy does not mention ``include_keywords``.
#: Lives here because both the filters and the scorer need it (and ``app.config``
#: is a leaf module, so importing it never creates a cycle).
ROLE_MISMATCH_FLAG = "роль не из include_keywords"


class Filters(BaseModel):
    include_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    exclude_companies: list[str] = Field(default_factory=list)
    sanctions_blocklist: list[str] = Field(default_factory=list)
    #: ``False`` (default): a role outside ``include_keywords`` only *flags* the
    #: vacancy and the scorer penalises it, so ``min_score`` makes the call.
    #: ``True`` restores the strict drop.
    role_strict: bool = False
    #: How many characters of the description head are scanned for a role match.
    role_scan_chars: int = 1200


def role_matches(title: str, description: str, f: Filters) -> bool:
    """Does the vacancy mention one of ``include_keywords``?

    The title decides first; the head of the description is only a fallback,
    because ATS boards often put the role in the body («…for an SRE team»).
    Lives here (not in ``app.pipeline.filters``) so the scorer can reuse it
    without creating an import cycle between ``app.scoring`` and ``app.pipeline``.
    """
    if not f.include_keywords:
        return True
    low_title = title.casefold()
    low_head = f"{title} {description}".casefold()[: f.role_scan_chars]
    return any(
        needle in low_title or needle in low_head
        for needle in (k.casefold() for k in f.include_keywords)
    )


class ScoringWeights(BaseModel):
    """Weights for the transparent rule-based scorer (total caps at 100)."""

    stack_core_each: float = 6.0
    stack_core_cap: float = 36.0
    stack_plus_each: float = 2.5
    stack_plus_cap: float = 12.0
    domain_match: float = 14.0
    seniority_match: float = 12.0
    salary_at_or_above_target: float = 14.0
    salary_between_min_and_target: float = 9.0
    salary_below_min: float = -18.0
    salary_unknown: float = 4.0
    russian_speaking_signal: float = 8.0
    penalty_onsite: float = -25.0
    penalty_irrelevant_role: float = -20.0
    penalty_residency: float = -6.0
    penalty_scam_suspect: float = -10.0
    geo_restricted: float = -4.0


class Profile(BaseModel):
    candidate: Candidate = Field(default_factory=Candidate)
    filters: Filters = Field(default_factory=Filters)
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    #: Minimum score to be considered for LLM re-scoring / digest.
    min_score: int = 40
    alert_score: int = 85


class SourceConfig(BaseModel):
    name: str
    kind: Literal[
        "greenhouse", "lever", "ashby", "remoteok", "wwr_rss", "hh", "habr", "telegram"
    ]
    enabled: bool = True
    token: str | None = None
    url: str | None = None
    company: str | None = None
    queries: list[str] = Field(default_factory=list)
    note: str = ""

    @property
    def label(self) -> str:
        return self.company or self.token or self.name


class SourcesConfig(BaseModel):
    sources: list[SourceConfig] = Field(default_factory=list)
    telegram_channels: list[str] = Field(default_factory=list)


class Settings(BaseSettings):
    """Environment-driven settings. Real variable names come from ``.env``."""

    # ``.env`` next to the repo (host development) or inside it (Docker image).
    # Missing files are skipped by pydantic-settings, so both may coexist.
    model_config = SettingsConfigDict(
        env_file=(str(APP_ROOT / ".env"), str(APP_ROOT.parent / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram (names as present in .env) ---
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- LLM (Gemini, added by the customer) ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    llm_daily_budget_usd: float = 0.50

    # --- Optional Telethon / IMAP (absent in .env by default) ---
    tg_api_id: int | None = None
    tg_api_hash: str = ""
    tg_session_path: str = "data/jobhunter-telethon"

    # --- Runtime ---
    timezone: str = "Europe/Moscow"
    db_path: str = "data/jobhunter.db"
    backup_dir: str = "data/backups"
    backup_keep: int = 7
    log_level: str = "INFO"
    log_json: bool = False
    digest_time: str = "09:00"
    weekly_report_weekday: int = 0  # Monday
    alerts_per_hour: int = 3
    http_timeout: float = 20.0
    http_max_retries: int = 3
    user_agent: str = "jobhunter/0.1 (+https://t.me/atenboro; ivan.drozdov.devops@gmail.com)"
    fetch_concurrency: int = 4
    domain_min_interval: float = 1.2
    fetch_interval_minutes: int = 60
    healthz_port: int = 8080
    followup_days: int = 6

    @property
    def llm_available(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def telegram_available(self) -> bool:
        return bool(self.telegram_enabled and self.telegram_bot_token and self.telegram_chat_id)

    def owner_chat_ids(self) -> set[int]:
        """Whitelist: the bot answers only these chat ids."""
        out: set[int] = set()
        for raw in str(self.telegram_chat_id).replace(";", ",").split(","):
            raw = raw.strip()
            if raw.lstrip("-").isdigit():
                out.add(int(raw))
        return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def load_profile(path: str | None = None) -> Profile:
    data = _load_yaml(Path(path) if path else APP_ROOT / "config" / "profile.yaml")
    return Profile.model_validate(data) if data else Profile()


@lru_cache(maxsize=1)
def load_sources(path: str | None = None) -> SourcesConfig:
    data = _load_yaml(Path(path) if path else APP_ROOT / "config" / "sources.yaml")
    return SourcesConfig.model_validate(data) if data else SourcesConfig()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
