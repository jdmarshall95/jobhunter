"""Ashby posting API.

Endpoint (verified 200 for ``blockdaemon``, ``coinbase``):
``https://api.ashbyhq.com/posting-api/job-board/{name}``
"""

from __future__ import annotations

from typing import Any

from app.models import RawJob
from app.sources.base import SourcePlugin

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{name}"


def parse_ashby(payload: dict[str, Any], *, company: str, source: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in payload.get("jobs") or []:
        title = str(item.get("title") or "").strip()
        url = str(item.get("jobUrl") or item.get("applyUrl") or "").strip()
        if not title or not url:
            continue
        comp = item.get("location") or ""
        if isinstance(comp, dict):
            comp = comp.get("location") or ""
        tech = item.get("techStack") or []
        description = str(item.get("descriptionHtml") or item.get("descriptionPlain") or "")
        if tech and isinstance(tech, list):
            description = f"Stack: {', '.join(str(t) for t in tech)}\n{description}"
        jobs.append(
            RawJob(
                source=source,
                company=str(item.get("company") or company),
                title=title,
                url=url,
                location=str(comp or item.get("secondaryLocation") or ""),
                remote=bool(item.get("isRemote")) if "isRemote" in item else None,
                description=description,
                posted_at=item.get("publishedAt") or item.get("firstPublished"),  # type: ignore[arg-type]
                raw={"id": item.get("id"), "department": item.get("department")},
            )
        )
    return jobs


class AshbySource(SourcePlugin):
    kind = "ashby"

    async def fetch(self) -> list[RawJob]:
        name = self.cfg.token or self.cfg.name
        payload = await self._get_json(BASE_URL.format(name=name))
        if not isinstance(payload, dict):
            raise self._fail("unexpected payload shape")
        return parse_ashby(payload, company=self.cfg.company or name, source=self.name)


__all__ = ["AshbySource", "parse_ashby", "BASE_URL"]
