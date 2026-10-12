"""Greenhouse public job board API.

Endpoint (verified during reconnaissance):
``https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true``

Only tokens confirmed with HTTP 200 are enabled in ``config/sources.yaml``;
unverified guesses are kept commented out instead of silently failing forever.
"""

from __future__ import annotations

from typing import Any

from app.models import RawJob
from app.sources.base import SourcePlugin

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _location(raw: dict[str, Any]) -> str:
    loc = raw.get("location") or {}
    if isinstance(loc, dict):
        return str(loc.get("name") or "")
    return str(loc or "")


def parse_greenhouse(payload: dict[str, Any], *, company: str, source: str) -> list[RawJob]:
    """Pure parser — easy to unit-test without the network."""
    jobs: list[RawJob] = []
    for item in payload.get("jobs") or []:
        title = str(item.get("title") or "").strip()
        url = str(item.get("absolute_url") or "").strip()
        if not title or not url:
            continue
        content = str(item.get("content") or item.get("description") or "")
        jobs.append(
            RawJob(
                source=source,
                company=company,
                title=title,
                url=url,
                location=_location(item),
                description=content,
                posted_at=item.get("updated_at") or item.get("first_published"),  # type: ignore[arg-type]
                raw={"id": item.get("id"), "type": item.get("type", "")},
            )
        )
    return jobs


class GreenhouseSource(SourcePlugin):
    kind = "greenhouse"

    async def fetch(self) -> list[RawJob]:
        token = self.cfg.token or self.cfg.name
        url = BASE_URL.format(token=token)
        payload = await self._get_json(url)
        if not isinstance(payload, dict):
            raise self._fail("unexpected payload shape")
        return parse_greenhouse(payload, company=self.cfg.company or token, source=self.name)


__all__ = ["GreenhouseSource", "parse_greenhouse", "BASE_URL"]
