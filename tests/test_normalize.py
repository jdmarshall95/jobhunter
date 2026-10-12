"""Normalize: ``RawJob`` -> ``Job`` (ТЗ §6 «Normalize»)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.models import canonical_url, content_hash
from app.pipeline.normalize import (
    age_days,
    detect_remote,
    normalize,
    parse_posted,
    parse_salary,
    strip_html,
)


class TestCanonicalUrl:
    def test_strips_utm_and_ref(self) -> None:
        assert canonical_url("https://jobs.x.io/1?utm_source=tg&ref=a") == "https://jobs.x.io/1"

    def test_strips_gh_jid(self) -> None:
        assert canonical_url("https://a.io/jobs/1?gh_jid=42") == "https://a.io/jobs/1"

    def test_keeps_business_params(self) -> None:
        assert canonical_url("https://a.io/j?mode=x") == "https://a.io/j?mode=x"

    def test_strips_trailing_slash(self) -> None:
        assert canonical_url("https://a.io/j/") == "https://a.io/j"

    def test_empty(self) -> None:
        assert canonical_url("") == ""


class TestStripHtml:
    def test_removes_tags_and_unescapes(self) -> None:
        assert strip_html("<p>Hi & bye</p>") == "Hi & bye"

    def test_collapses_whitespace(self) -> None:
        assert strip_html("plain   text") == "plain text"

    def test_plain_text_untouched(self) -> None:
        assert strip_html("no markup here") == "no markup here"


class TestDetectRemote:
    def test_remote_token(self) -> None:
        assert detect_remote("Remote — anywhere") is True

    def test_onsite_wins(self) -> None:
        assert detect_remote("В офисе, Москва") is False

    def test_unknown(self) -> None:
        assert detect_remote("") is None

    def test_multiple_texts(self) -> None:
        assert detect_remote("somewhere", "fully remote") is True


class TestParseSalary:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("$4,200 monthly, fully remote.", (4200.0, None, "USD")),
            ("$3,000 - $4,500 / month", (3000.0, 4500.0, "USD")),
            ("Salary $2,800", (2800.0, None, "USD")),
            ("$4k/mo", (4000.0, None, "USD")),
            ("€60k", (5000.0, None, "EUR")),
            ("3-4k USD", (3000.0, 4000.0, "USD")),
            ("Вилка: 3000–4500 USDT", (3000.0, 4500.0, "USD")),
            ("60 000 – 72 000 USD per year", (5000.0, 6000.0, "USD")),
            ("Kubernetes, Terraform, Ansible and Prometheus.", (None, None, "")),
            ("2019-2023", (None, None, "")),
            ("120000-150000", (None, None, "")),
            ("₴30 000 – 45 000", (None, None, "")),
        ],
    )
    def test_cases(self, text: str, expected: tuple[float | None, float | None, str]) -> None:
        assert parse_salary(text) == expected

    def test_yearly_is_monthly(self) -> None:
        low, high, currency = parse_salary("up to $95,000")
        assert currency == "USD"
        assert low == pytest.approx(7916.67, abs=0.1)
        assert high is None


class TestParsePosted:
    def test_iso(self) -> None:
        assert parse_posted("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    def test_epoch_millis(self) -> None:
        assert parse_posted(1767225845000) == datetime(2026, 1, 1, 0, 4, 5, tzinfo=UTC)

    def test_epoch_seconds(self) -> None:
        assert parse_posted(1767225845) == datetime(2026, 1, 1, 0, 4, 5, tzinfo=UTC)

    def test_junk(self) -> None:
        assert parse_posted("not a date") is None

    def test_none(self) -> None:
        assert parse_posted(None) is None


class TestNormalize:
    def test_good_raw(self, good_raw: Any) -> None:
        job = normalize(good_raw)
        assert job.remote is True
        assert job.salary_min == 4200.0
        assert job.salary_max is None
        assert job.currency == "USD"
        assert job.geo_restricted is False
        assert job.sources == ["gh"]
        assert job.url == "https://a.io/jobs/1"
        assert "<b>" not in job.description

    def test_id_is_content_hash(self, good_raw: Any) -> None:
        job = normalize(good_raw)
        assert job.id == content_hash("CryptoCo", "Senior DevOps Engineer", "Remote (EMEA)")
        assert job.id == "4d3694da559eb861"

    def test_url_is_canonical(self) -> None:
        job = normalize(make_raw_local(url="https://a.io/jobs/1?utm_source=tg"))
        assert job.url == "https://a.io/jobs/1"

    def test_missing_company_and_title_get_placeholders(self) -> None:
        job = normalize(make_raw_local(company="", title=""))
        assert job.company == "—"
        assert job.title == "—"

    def test_geo_restriction_detected(self) -> None:
        job = normalize(
            make_raw_local(description="Senior DevOps Engineer. Must be located in Germany.")
        )
        assert job.geo_restricted is True

    def test_remote_falls_back_to_text(self) -> None:
        job = normalize(make_raw_local(description="100% remote, anywhere in the world"))
        assert job.remote is True

    def test_first_seen_is_now(self) -> None:
        job = normalize(make_raw_local())
        assert job.first_seen_at is not None
        assert age_days(job) == pytest.approx(0.0, abs=0.01)


def make_raw_local(**kwargs: Any) -> Any:
    from app.models import RawJob

    base: dict[str, Any] = {
        "source": "gh",
        "company": "CryptoCo",
        "title": "Senior DevOps Engineer",
        "url": "https://a.io/jobs/1",
    }
    base.update(kwargs)
    return RawJob(**base)
