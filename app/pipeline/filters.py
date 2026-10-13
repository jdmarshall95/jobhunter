"""Hard filters: remote-only, role keywords, exclude keywords, blocklists.

A filter never silently drops a geo-restricted vacancy: it is kept but flagged,
so the customer can see *why* something looked odd.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import (
    ROLE_MISMATCH_FLAG,
    Filters,
    Profile,
    role_matches,
)
from app.models import Job

#: Titles that are clearly not a DevOps/SRE/platform role.
NON_ROLE_TOKENS = (
    "senior",
    "staff",
    "principal",
    "lead",
    "middle",
    "mid",
    "junior",
    "head",
    "engineer",
    "engineering",
)


@dataclass(slots=True)
class FilterOutcome:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    #: geo-restricted but otherwise fine -> keep with a flag
    flags: list[str] = field(default_factory=list)


def _blob(job: Job) -> str:
    return f"{job.title} {job.description}".casefold()


def _title_blob(job: Job) -> str:
    return job.title.casefold()


def apply_hard_filters(
    job: Job,
    profile: Profile,
    extra_stopwords: list[str] | None = None,
    blocklist: list[str] | None = None,
) -> FilterOutcome:
    f: Filters = profile.filters
    title = _title_blob(job)
    blob = _blob(job)
    reasons: list[str] = []
    flags: list[str] = []

    # 1. Role keywords: title first, then the head of the description.
    include = [k.casefold() for k in f.include_keywords]
    if not role_matches(job.title, job.description, f):
        # Soft by default: the scorer applies ``penalty_irrelevant_role`` and
        # ``min_score`` makes the call. A hard drop here once cost us 883 of
        # 898 vacancies, because ATS boards title roles «Manager»/«Specialist».
        if f.role_strict:
            reasons.append(ROLE_MISMATCH_FLAG)
        else:
            flags.append(ROLE_MISMATCH_FLAG)

    # 2. Remote-only.
    if profile.candidate.work_mode == "remote" and job.remote is False:
        reasons.append("не remote")
    if job.geo_restricted:
        flags.append("гео-ограничение")

    # 3. Exclude keywords (title is decisive, body only for strong tokens).
    # Luna Park often lists several roles in one tl;dr («AI, frontend, full-stack
    # and other engineers») — a secondary «frontend» must not kill the whole card
    # when a platform/infra/devops angle is also present.
    _MULTI_ROLE_RESCUE = (
        "devops",
        "sre",
        "platform",
        "infra",
        "backend",
        "full-stack",
        "fullstack",
        "full stack",
        "other engineers",
        "mlops",
        "reliability",
    )
    for kw in f.exclude_keywords:
        low = kw.casefold()
        if low in title:
            if low == "frontend" and any(tok in title for tok in _MULTI_ROLE_RESCUE):
                flags.append("frontend в списке ролей")
            else:
                reasons.append(f"стоп-слово «{kw}» в заголовке")
        elif low in ("junior", "intern", "trainee", "data scientist") and low in blob[:200]:
            reasons.append(f"стоп-слово «{kw}»")

    for kw in extra_stopwords or []:
        if kw and kw.casefold() in title:
            reasons.append(f"стоп-слово из бота «{kw}»")

    # 4. Company blocklist: static (profile.yaml) + bot-managed (/block).
    company = job.company.casefold()
    for blocked in [*f.exclude_companies, *(blocklist or [])]:
        if blocked and blocked.casefold() in company:
            reasons.append(f"компания в блоке «{blocked}»")

    # 5. Sanity: a title that is not an engineering role at all.
    if not any(tok in title for tok in NON_ROLE_TOKENS) and not any(
        k in title for k in include
    ):
        flags.append("роль не похожа на инженерную")

    return FilterOutcome(passed=not reasons, reasons=reasons, flags=flags)


def sanctions_warning(job: Job, blocklist: list[str]) -> str | None:
    """Sanction-risk companies are *warned about*, not silently cut."""
    for name in blocklist:
        if name and name.casefold() in job.company.casefold():
            return f"⚠️ санкционный риск: {name}"
    return None
