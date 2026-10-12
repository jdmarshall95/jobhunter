"""Source plugins.

Every source is a small class with one job: ``async def fetch() -> list[RawJob]``.
Network access goes through :class:`app.http.HttpFetcher` (throttling, retries,
ETag cache). Errors are *not* swallowed here — the runner isolates them so one
broken source can never kill the whole run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.config import SourceConfig
from app.http import HttpFetcher
from app.logging_setup import get_logger
from app.models import RawJob

log = get_logger(__name__)


class SourceError(RuntimeError):
    """Raised when a source could not deliver data (HTTP failure, bad payload)."""


class SourcePlugin(ABC):
    """Base class: config + shared HTTP client + a human-readable label."""

    kind: str = "base"

    def __init__(self, cfg: SourceConfig, http: HttpFetcher) -> None:
        self.cfg = cfg
        self.http = http

    @property
    def name(self) -> str:
        return self.cfg.name or self.kind

    @property
    def label(self) -> str:
        return self.cfg.label

    @abstractmethod
    async def fetch(self) -> list[RawJob]:
        """Return raw vacancies. Must raise :class:`SourceError` on failure."""

    def _fail(self, message: str, status: int = 0) -> SourceError:
        suffix = f" (http {status})" if status else ""
        return SourceError(f"{self.name}: {message}{suffix}")

    async def _get_json(self, url: str, **kwargs: object) -> object:
        payload, status = await self.http.get_json(url, **kwargs)
        if payload is None:
            raise self._fail("no data", status)
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name}>"


__all__ = ["SourceError", "SourcePlugin"]
