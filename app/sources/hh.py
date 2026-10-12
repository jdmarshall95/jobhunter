"""hh.ru public API (``https://api.hh.ru/vacancies``).

Reconnaissance note: from this host the API answers **403 forbidden** for every
User-Agent we tried (Mozilla → 403, ``jobhunter/0.1`` → 400 ``blacklisted``,
``curl/8.5.0`` → 403), while the HTML site is 200. The source therefore ships
enabled but is expected to report a health error here; the runner isolates it.

``HH-User-Agent`` is mandatory per the API docs.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from app.models import RawJob
from app.sources.base import SourcePlugin

API_URL = "https://api.hh.ru/vacancies"
PER_PAGE = 50
MAX_PAGES = 2


def parse_hh(payload: dict[str, Any], *, source: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in payload.get("items") or []:
        title = str(item.get("name") or "").strip()
        url = str(item.get("alternate_url") or "").strip()
        if not title or not url:
            continue
        area = item.get("area") or {}
        schedule = (item.get("schedule") or {}).get("name") or ""
        dep = item.get("department") or ""
        raw_roles = item.get("professional_roles") or []
        role_names = (str(n.get("name")) for n in raw_roles if n.get("name"))
        roles = ", ".join(role_names)
        jobs.append(
            RawJob(
                source=source,
                company=str((item.get("employer") or {}).get("name") or "hh.ru"),
                title=title,
                url=url,
                location=str(area.get("name") or ""),
                remote=schedule.casefold() == "удаленная работа" or None,
                description=str(item.get("snippet", {}).get("requirement") or "")
                + "\n"
                + str(item.get("snippet", {}).get("responsibility") or ""),
                posted_at=item.get("published_at"),  # type: ignore[arg-type]
                raw={
                    "id": item.get("id"),
                    "department": dep,
                    "roles": roles,
                    "area": area.get("id"),
                },
            )
        )
    return jobs


class HHSource(SourcePlugin):
    kind = "hh"

    async def fetch(self) -> list[RawJob]:
        params: dict[str, Any] = {
            "search": " OR ".join(self.cfg.queries) or "DevOps",
            "schedule": "remote",
            "per_page": PER_PAGE,
            "page": 0,
            "order_by": "publication_distance",
        }
        url = f"{API_URL}?{urlencode(params)}"
        payload = await self._get_json(url, headers={"HH-User-Agent": self.http_user_agent})
        if not isinstance(payload, dict):
            raise self._fail("unexpected payload shape")
        return parse_hh(payload, source=self.name)

    @property
    def http_user_agent(self) -> str:
        return self.cfg.note or "jobhunter/0.1 (ivan.drozdov.devops@gmail.com)"


__all__ = ["HHSource", "parse_hh", "API_URL"]
