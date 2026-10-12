"""WeWorkRemotely RSS (verified 200 for the programming category feed).

``https://weworkremotely.com/categories/remote-programming-jobs.rss``

The feed is broad, so ``queries`` from ``config/sources.yaml`` act as a cheap
pre-filter before the shared hard filters run.
"""

from __future__ import annotations

from typing import Any

import feedparser

from app.models import RawJob
from app.sources.base import SourcePlugin

FEED_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"


def parse_wwr(
    feed: dict[str, Any], *, source: str, queries: list[str] | None = None
) -> list[RawJob]:
    entries = feed.get("entries") or []
    needles = [q.casefold() for q in (queries or []) if q]
    jobs: list[RawJob] = []
    for e in entries:
        title = str(e.get("title") or "").strip()
        url = str(e.get("link") or "").strip()
        if not title or not url:
            continue
        blob = f"{title} {e.get('summary') or ''}".casefold()
        if needles and not any(n in blob for n in needles):
            continue
        company = str(e.get("wfw_company") or "").strip()
        if not company:
            # Title usually looks like "Company: Role" — fall back to that.
            head, sep, tail = title.partition(":")
            company = head.strip() if sep else "WeWorkRemotely"
        jobs.append(
            RawJob(
                source=source,
                company=company,
                title=title,
                url=url,
                location="Remote",
                remote=True,
                description=str(e.get("summary") or e.get("description") or ""),
                posted_at=e.get("published") or e.get("updated"),  # type: ignore[arg-type]
                raw={"id": e.get("id"), "category": e.get("wfw_category")},
            )
        )
    return jobs


class WWRSource(SourcePlugin):
    kind = "wwr_rss"

    async def fetch(self) -> list[RawJob]:
        url = self.cfg.url or FEED_URL
        body, status = await self.http.get_text(url)
        if body is None:
            raise self._fail("no data", status)
        feed = feedparser.parse(body)
        if not feed.get("entries"):
            raise self._fail("feed has no entries")
        return parse_wwr(feed, source=self.name, queries=self.cfg.queries)


__all__ = ["WWRSource", "parse_wwr", "FEED_URL"]
