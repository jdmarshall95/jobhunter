"""Dedup: one vacancy seen in two sources must become one card.

Two keys are used: the content hash ``company+title+location`` (``Job.id``) and
the canonical URL. The first occurrence wins; later ones merge their source
name into ``sources`` and contribute a missing salary/description.
"""

from __future__ import annotations

from app.models import Job, canonical_url


def _merge(target: Job, other: Job) -> None:
    for src in [other.source, *other.sources]:
        if src and src not in target.sources:
            target.sources.append(src)
    if not target.description and other.description:
        target.description = other.description
    if target.salary_min is None and other.salary_min is not None:
        target.salary_min, target.salary_max, target.currency = (
            other.salary_min,
            other.salary_max,
            other.currency,
        )
    if target.remote is None and other.remote is not None:
        target.remote = other.remote
    if target.posted_at is None and other.posted_at is not None:
        target.posted_at = other.posted_at
    if not target.location and other.location:
        target.location = other.location


def dedupe(jobs: list[Job]) -> list[Job]:
    """Collapse duplicates, preserving first-seen order."""
    by_content: dict[str, Job] = {}
    by_url: dict[str, Job] = {}
    result: list[Job] = []

    for job in jobs:
        canon = canonical_url(job.url)
        existing = by_content.get(job.id)
        if existing is None and canon:
            existing = by_url.get(canon)
        if existing is not None:
            _merge(existing, job)
            continue
        by_content[job.id] = job
        if canon:
            by_url[canon] = job
        result.append(job)
    return result
