"""Timezone helpers: every user-visible timestamp is shown in ``TIMEZONE``.

Storage keeps UTC (``now_iso()``), the scheduler runs on ``Settings.timezone``
and the bot must not contradict either of them — a digest dated in UTC would
arrive at 09:00 MSK with yesterday's date on it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: Fallback when ``TIMEZONE`` holds something the system cannot resolve.
FALLBACK_TZ = "UTC"


def zone(name: str | None) -> ZoneInfo:
    """Resolve an IANA zone, falling back to UTC instead of raising."""
    if not name:
        return ZoneInfo(FALLBACK_TZ)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - bad config
        return ZoneInfo(FALLBACK_TZ)


def now_in(name: str | None) -> datetime:
    """Current time in ``name`` (UTC when the zone is unknown)."""
    return datetime.now(tz=zone(name))


def to_local(value: object, name: str | None, fmt: str = "%d.%m %H:%M") -> str:
    """Render a stored ISO-8601 UTC timestamp in the local zone.

    Anything unparsable is returned untouched (truncated to the minute) so a
    odd row never breaks a whole message.
    """
    raw = str(value or "").strip()
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:  # pragma: no cover - defensive
        return raw[:16].replace("T", " ")
    if parsed.tzinfo is None:  # naive rows are UTC by convention
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(zone(name)).strftime(fmt)


__all__ = ["FALLBACK_TZ", "now_in", "to_local", "zone"]
