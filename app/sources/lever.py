"""Lever public postings API.

Endpoint: ``https://api.lever.co/v0/postings/{slug}?mode=json``

Reconnaissance note: the endpoint itself is alive, but none of the crypto slugs
we tried returned 200, so Lever sources ship disabled until the customer
confirms a real slug (see ``config/sources.yaml``).
"""

from __future__ import annotations

from typing import Any

from app.models import RawJob
from app.sources.base import SourcePlugin

BASE_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _remote_flag(value: object) -> bool | None:
    """Tri-state: ``remote``/``unrestricted`` -> True, ``office`` -> False, else None."""
    text = str(value or "").strip().casefold()
    if text in ("remote", "unrestricted"):
        return True
    if text in ("office", "hybrid"):
        return False
    return None


def parse_lever(payload: list[dict[str, Any]], *, company: str, source: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in payload or []:
        title = str(item.get("text") or "").strip()
        url = str(item.get("hostedUrl") or item.get("applyUrl") or "").strip()
        if not title or not url:
            continue
        cats = item.get("categories") or {}
        lists = item.get("lists") or {}
        description = str(lists.get("descriptionPlain") or item.get("description") or "")
        jobs.append(
            RawJob(
                source=source,
                company=company,
                title=title,
                url=url,
                location=str(cats.get("location") or ""),
                remote=_remote_flag(cats.get("workplaceType")),
                description=description,
                posted_at=item.get("createdAt"),  # epoch millis
                raw={"id": item.get("id"), "team": cats.get("team")},
            )
        )
    return jobs


class LeverSource(SourcePlugin):
    kind = "lever"

    async def fetch(self) -> list[RawJob]:
        slug = self.cfg.token or self.cfg.name
        payload = await self._get_json(BASE_URL.format(slug=slug))
        if not isinstance(payload, list):
            raise self._fail("unexpected payload shape")
        return parse_lever(payload, company=self.cfg.company or slug, source=self.name)


__all__ = ["LeverSource", "parse_lever", "BASE_URL"]
