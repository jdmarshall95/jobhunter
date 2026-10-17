"""Transparent rule-based scoring (0–100) with a visible breakdown.

Every point added or removed is recorded as ``(label, delta)`` so the Telegram
card can answer «почему эта вакансия подошла». No hidden magic, no LLM here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import (
    ROLE_MISMATCH_FLAG,
    Profile,
    SalaryExpectation,
    role_matches,
)
from app.models import GeoScope, Job

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

_WORD = re.compile(r"[a-z0-9+#\.]{2,}")

#: Terms that betray a likely scam / low-quality posting.
SCAM_SIGNALS = (
    "оплата за обучение",
    "training fee",
    "unpaid",
    "stipend",
    "pay to work",
    "your own wallet",
    "own wallet",
    "passport scan",
)

RESIDENCY_SIGNALS = (
    "citizenship",
    "гражданство",
    "residency",
    "resident",
    "work permit",
    "visa required",
)

RUSSIAN_SIGNALS = (
    "russian-speaking",
    "russian speaking",
    "русскоязыч",
    "русский",
    "cis",
    "english is a plus",
)

#: Non-USD monthly figures are converted before comparison with the expectation.
DEFAULT_RATES_TO_USD = {
    "USD": 1.0,
    "USDT": 1.0,
    "USDC": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "RUB": 0.011,
    "UAH": 0.024,
    "PLN": 0.25,
    "BTC": 65000.0,
    "ETH": 3200.0,
}


@dataclass(slots=True)
class ScoreResult:
    score: float
    breakdown: list[tuple[str, float]]
    flags: list[str]

    def to_text(self, limit: int = 6) -> str:
        parts = [f"{label} {'+' if delta >= 0 else ''}{delta:g}" for label, delta in self.breakdown]
        return "; ".join(parts[:limit])


class FxCache:
    """Minimal currency cache. Real rates are optional; defaults are static."""

    def __init__(self, rates: dict[str, float] | None = None) -> None:
        self.rates = dict(DEFAULT_RATES_TO_USD)
        if rates:
            self.rates.update(rates)

    def to_usd(self, amount: float, currency: str) -> float:
        return amount * self.rates.get((currency or "USD").upper(), 1.0)


def _token_hits(needles: list[str], haystack: str, head_len: int = 500) -> dict[str, int]:
    """Count occurrences, weighting mentions in the first ``head_len`` chars x2."""
    hits: dict[str, int] = {}
    head = haystack[:head_len]
    for needle in needles:
        low = needle.casefold()
        if not low:
            continue
        count = haystack.count(low)
        if count:
            count += head.count(low)  # extra weight for early mentions
        if count:
            hits[needle] = count
    return hits


def salary_detail(usd: float, exp: SalaryExpectation) -> str:
    """Vacancy salary with its gross and annual equivalents.

    The customer quotes «3–4k» **net** («на руки»), so a vacancy figure is read
    as net as well and grossed up with the averaged ``tax_rate``; the yearly
    number answers «сколько это в годовых».
    """
    if not exp.net:
        return f"${usd:,.0f} gross · ${usd * 12:,.0f}/год"
    gross = exp.to_gross(usd)
    return f"${usd:,.0f} net · ≈${gross:,.0f} gross · ${gross * 12:,.0f}/год"


def score_job(
    job: Job,
    profile: Profile,
    fx: FxCache | None = None,
    *,
    negative_signals: dict[str, int] | None = None,
) -> ScoreResult:
    """Compute score + breakdown. ``negative_signals`` comes from reject feedback."""
    fx = fx or FxCache()
    w = profile.scoring
    cand = profile.candidate
    haystack = f"{job.title} {job.description}".casefold()
    breakdown: list[tuple[str, float]] = []
    flags: list[str] = []

    def add(label: str, delta: float) -> None:
        if delta:
            breakdown.append((label, round(delta, 1)))

    # --- stack ---------------------------------------------------------
    core = _token_hits(cand.stack_core, haystack)
    if core:
        core_delta = min(w.stack_core_cap, w.stack_core_each * len(core))
        add(f"стек ядра: {', '.join(sorted(core)[:4])}", core_delta)
    plus = _token_hits(cand.stack_plus, haystack)
    if plus:
        plus_delta = min(w.stack_plus_cap, w.stack_plus_each * len(plus))
        add(f"доп. стек: {', '.join(sorted(plus)[:4])}", plus_delta)

    # --- domain --------------------------------------------------------
    domain = [d for d in cand.domains_priority if d.casefold() in haystack]
    if domain:
        add(f"домен: {domain[0]}", w.domain_match)

    # --- seniority -----------------------------------------------------
    if any(s in haystack[:300] for s in cand.seniority):
        add("грейд middle/senior/lead", w.seniority_match)
    elif "junior" in haystack[:300]:
        add("грейд junior", -w.seniority_match)

    # --- salary --------------------------------------------------------
    monthly = job.salary_max if job.salary_max is not None else job.salary_min
    if monthly is None:
        add("вилка не указана", w.salary_unknown)
    else:
        usd = fx.to_usd(monthly, job.currency)
        exp = cand.salary_expectation_usd_month
        detail = salary_detail(usd, exp)
        if usd >= exp.target:
            add(f"вилка ≥ цели ({detail})", w.salary_at_or_above_target)
        elif usd >= exp.min:
            add(f"вилка в диапазоне ({detail})", w.salary_between_min_and_target)
        else:
            add(f"вилка ниже минимума ({detail})", w.salary_below_min)

    # --- russian-speaking team (plus-signal, not a filter) --------------
    ru_hits = [s for s in RUSSIAN_SIGNALS if s in haystack]
    if ru_hits:
        add("русскоязычная команда", w.russian_speaking_signal)
    elif cand.want_russian_speaking_team:
        flags.append("RU-сигнал не найден")

    # --- penalties ------------------------------------------------------
    # The role filter is soft by default (see app/pipeline/filters.py), so the
    # penalty for an off-profile role is applied here instead of a hard drop.
    if not role_matches(job.title, job.description, profile.filters):
        add(ROLE_MISMATCH_FLAG, w.penalty_irrelevant_role)
    if job.remote is False:
        add("не remote", w.penalty_onsite)
    if job.geo_restricted:
        add("гео-ограничение", w.geo_restricted)
    # Geo scope: the customer wants *worldwide* remote. A US-only job stays in
    # the feed but takes a heavy hit; a city/region-scoped one a lighter one.
    # ``LOCAL`` is already covered by ``penalty_onsite`` above.
    if job.geo_scope is GeoScope.USA_ONLY:
        add("гео: только USA", w.penalty_usa_only)
    elif job.geo_scope in (GeoScope.COUNTRY, GeoScope.REGION):
        place = job.geo_place or job.location
        add(f"не worldwide remote ({place})", w.penalty_not_worldwide)
    if any(s in haystack for s in RESIDENCY_SIGNALS):
        add("нужно резидентство/разрешение", w.penalty_residency)

    scam = [s for s in SCAM_SIGNALS if s in haystack]
    if scam:
        add("подозрение на скам", w.penalty_scam_suspect)
        flags.append("suspicious")
        job.suspicious = True

    # --- feedback loop from repeated rejects ---------------------------
    for token, count in (negative_signals or {}).items():
        if count >= 3 and token.casefold() in haystack:
            add(f"ты часто отклонял «{token}»", -4.0)

    total = sum(delta for _, delta in breakdown)
    job.score = max(0.0, min(100.0, total))
    job.score_breakdown = breakdown
    job.flags = flags
    return ScoreResult(job.score, breakdown, flags)
