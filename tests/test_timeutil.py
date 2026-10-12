"""Часовые пояса пользовательских меток (``app.timeutil``).

Хранилище пишет UTC, планировщик живёт в ``TIMEZONE`` — бот обязан показывать
то же самое время, иначе дайджест в 09:00 МСК придёт со вчерашней датой.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.timeutil import now_in, to_local, zone


class TestZone:
    def test_known_zone(self) -> None:
        assert str(zone("Europe/Moscow")) == "Europe/Moscow"

    @pytest.mark.parametrize("name", [None, "", "Mars/Olympus"])
    def test_unknown_zone_falls_back_to_utc(self, name: str | None) -> None:
        assert str(zone(name)) == "UTC"


class TestNowIn:
    def test_now_in_is_offset(self) -> None:
        assert now_in("Europe/Moscow").utcoffset() is not None

    def test_now_in_utc_has_a_zero_offset(self) -> None:
        assert now_in("UTC").utcoffset() == timedelta(0)


class TestToLocal:
    def test_utc_iso_is_rendered_in_moscow(self) -> None:
        # 13:40 UTC is 16:40 MSK (UTC+3, no DST since 2014).
        assert to_local("2026-10-12T13:40:00+00:00", "Europe/Moscow") == "12.10 16:40"

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        assert to_local("2026-10-12T13:40:00", "UTC") == "12.10 13:40"

    def test_custom_format(self) -> None:
        assert to_local("2026-10-12T13:40:00+00:00", "UTC", fmt="%Y%m%d") == "20261012"

    def test_empty_value_is_a_dash(self) -> None:
        assert to_local("", "UTC") == "—"
        assert to_local(None, "UTC") == "—"

    def test_unparsable_value_survives(self) -> None:
        assert to_local("not-a-date", "UTC") == "not-a-date"

    def test_the_digest_date_follows_the_configured_zone(self) -> None:
        """Around midnight UTC the two zones can disagree — local wins."""
        local = now_in("Europe/Moscow").strftime("%d.%m")
        assert local == datetime.now(tz=zone("Europe/Moscow")).strftime("%d.%m")
