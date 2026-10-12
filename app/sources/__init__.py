"""Source registry: build plugins from ``config/sources.yaml``."""

from __future__ import annotations

from app.config import SourceConfig, SourcesConfig
from app.http import HttpFetcher
from app.logging_setup import get_logger
from app.sources.ashby import AshbySource
from app.sources.base import SourceError, SourcePlugin
from app.sources.greenhouse import GreenhouseSource
from app.sources.habr import HabrSource
from app.sources.hh import HHSource
from app.sources.lever import LeverSource
from app.sources.remoteok import RemoteOKSource
from app.sources.telegram import TelegramSource
from app.sources.wwr_rss import WWRSource

log = get_logger(__name__)

REGISTRY: dict[str, type[SourcePlugin]] = {
    GreenhouseSource.kind: GreenhouseSource,
    AshbySource.kind: AshbySource,
    LeverSource.kind: LeverSource,
    RemoteOKSource.kind: RemoteOKSource,
    WWRSource.kind: WWRSource,
    HHSource.kind: HHSource,
    HabrSource.kind: HabrSource,
    TelegramSource.kind: TelegramSource,
}


def build_source(cfg: SourceConfig, http: HttpFetcher) -> SourcePlugin | None:
    cls = REGISTRY.get(cfg.kind)
    if cls is None:
        log.warning("unknown source kind=%s name=%s — skipped", cfg.kind, cfg.name)
        return None
    return cls(cfg, http)


def build_sources(cfg: SourcesConfig, http: HttpFetcher) -> list[SourcePlugin]:
    plugins: list[SourcePlugin] = []
    for item in cfg.sources:
        if not item.enabled:
            log.info("source %s disabled in config", item.name)
            continue
        plugin = build_source(item, http)
        if plugin is not None:
            plugins.append(plugin)
    log.info("sources ready count=%d", len(plugins))
    return plugins


__all__ = [
    "REGISTRY",
    "SourceError",
    "SourcePlugin",
    "build_source",
    "build_sources",
    "AshbySource",
    "GreenhouseSource",
    "HabrSource",
    "HHSource",
    "LeverSource",
    "RemoteOKSource",
    "TelegramSource",
    "WWRSource",
]
