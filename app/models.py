"""Domain models: the single normalised ``Job`` schema plus scoring results."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

_TRACKING_PARAMS = re.compile(r"[?&](utm_[a-z]+|ref|referrer|src|source|gh_jid|shared_id)=[^&]*")
_WS = re.compile(r"\s+")


def canonical_url(url: str) -> str:
    """Strip tracking noise so the same vacancy from two sources dedups."""
    if not url:
        return ""
    url = url.strip().rstrip("/")
    prev = None
    while prev != url:  # repeatedly drop tracking params
        prev = url
        url = _TRACKING_PARAMS.sub("", url)
    return url.replace("?&", "?").replace("?#", "#").rstrip("?").rstrip("&")


def _norm(text: str) -> str:
    return _WS.sub(" ", (text or "").casefold()).strip()


@dataclass(slots=True)
class RawJob:
    """What a source plugin returns, before normalisation."""

    source: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str = ""
    description: str = ""
    posted_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class GeoScope(StrEnum):
    """How tightly a vacancy is tied to one geography.

    ``WORLDWIDE`` is what the customer is looking for; everything else stays in
    the feed but is penalised, because a «remote» job that only hires inside the
    USA is not really available to someone living in Europe.
    """

    #: Remote from anywhere ("Remote", "Worldwide", "Anywhere").
    WORLDWIDE = "worldwide"
    #: Remote from Europe/CIS — perfectly fine, no penalty.
    EUROPE = "europe"
    #: Remote inside a broad region that excludes Europe (APAC / LATAM / MENA).
    REGION = "region"
    #: Remote, but tied to a single country or city ("Remote - Singapore").
    COUNTRY = "country"
    #: USA-only: the customer cannot apply from Europe.
    USA_ONLY = "usa_only"
    #: Not remote at all — an office/hybrid job in a specific city.
    LOCAL = "local"
    #: Nothing to go on.
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Job:
    """Normalised vacancy. ``id`` is a stable content hash, not a DB row id."""

    source: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str = ""
    description: str = ""
    posted_at: datetime | None = None
    first_seen_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    db_id: int | None = None
    status: str = "new"
    score: float = 0.0
    score_breakdown: list[tuple[str, float]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    geo_restricted: bool = False
    #: How tightly the vacancy is tied to a geography (see :class:`GeoScope`).
    geo_scope: GeoScope = GeoScope.UNKNOWN
    #: Short human-readable place label ("USA", "Singapore", "Vienna").
    geo_place: str = ""
    suspicious: bool = False
    llm_verdict: str = ""
    llm_summary: str = ""
    notified_at: datetime | None = None
    followup_at: datetime | None = None

    @property
    def id(self) -> str:
        return content_hash(self.company, self.title, self.location)

    @property
    def salary_text(self) -> str:
        if self.salary_min is None and self.salary_max is None:
            return "не указана"
        cur = f" {self.currency}" if self.currency else ""
        if self.salary_min is not None and self.salary_max is not None:
            return f"{_fmt(self.salary_min)}–{_fmt(self.salary_max)}{cur}"
        only = self.salary_min if self.salary_min is not None else self.salary_max
        return f"от {_fmt(only)}{cur}" if self.salary_min else f"до {_fmt(only)}{cur}"

    @property
    def remote_text(self) -> str:
        if self.remote is True:
            return "remote"
        if self.remote is False:
            return f"не remote ({self.location or 'geo'})"
        return f"неясно ({self.location or 'без гео'})"


def content_hash(company: str, title: str, location: str) -> str:
    payload = "|".join((_norm(company), _norm(title), _norm(location)))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _fmt(value: float) -> str:
    return f"{int(value)}" if float(value).is_integer() else f"{value:.1f}"


class Status(StrEnum):
    NEW = "new"
    SAVED = "saved"
    APPLIED = "applied"
    SCREENING = "screening"
    TECH = "tech"
    OFFER = "offer"
    REJECTED = "rejected"
    IGNORED = "ignored"


STATUS_ORDER = [
    Status.NEW,
    Status.SAVED,
    Status.APPLIED,
    Status.SCREENING,
    Status.TECH,
    Status.OFFER,
    Status.REJECTED,
    Status.IGNORED,
]

#: Reasons offered by the ❌ button; they feed back into weights/stop-words.
REJECT_REASONS = {
    "not_remote": "не remote",
    "wrong_stack": "стек не тот",
    "low_salary": "низкая вилка",
    "company": "компания не нравится",
    "other": "другое",
}
