"""RemoteOK public JSON API (verified 200, ~567 KB payload).

``https://remoteok.com/api`` returns a list whose *first* element is an HTTP
header comment string — it has to be skipped.
"""

from __future__ import annotations

from typing import Any

from app.models import RawJob
from app.sources.base import SourcePlugin

API_URL = "https://remoteok.com/api"


def parse_remoteok(payload: list[Any], *, source: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue  # element 0 is a header comment
        title = str(item.get("position") or "").strip()
        url = str(item.get("url") or "").strip()
        if not title or not url:
            continue
        tags = [str(t) for t in (item.get("tags") or [])]
        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        jobs.append(
            RawJob(
                source=source,
                company=str(item.get("company") or "RemoteOK"),
                title=title,
                url=url,
                location=str(item.get("location") or ""),
                remote=True if not item.get("location") else None,
                salary_min=float(salary_min) if salary_min else None,
                salary_max=float(salary_max) if salary_max else None,
                currency=str(item.get("salary_currency") or "USD"),
                description=str(item.get("description") or "") or f"Tags: {', '.join(tags)}",
                posted_at=item.get("date"),  # type: ignore[arg-type]
                raw={"id": item.get("id"), "tags": tags[:12], "category": item.get("category")},
            )
        )
    return jobs


class RemoteOKSource(SourcePlugin):
    kind = "remoteok"

    async def fetch(self) -> list[RawJob]:
        payload = await self._get_json(API_URL)
        if not isinstance(payload, list):
            raise self._fail("unexpected payload shape")
        return parse_remoteok(payload, source=self.name)


__all__ = ["RemoteOKSource", "parse_remoteok", "API_URL"]
