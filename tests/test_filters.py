"""Hard filters: remote-only, роль, стоп-слова, блоклист компаний (ТЗ §6)."""

from __future__ import annotations

from typing import Any

import pytest

from app.pipeline.filters import FilterOutcome, apply_hard_filters, sanctions_warning
from tests.conftest import make_job


def good_job(**kwargs: Any) -> Any:
    """Vacancy that passes every filter of the test ``profile`` fixture."""
    base: dict[str, Any] = {
        "title": "Senior DevOps Engineer",
        "company": "CryptoCo",
        "description": "Kubernetes, Terraform and Prometheus at a crypto exchange.",
        "remote": True,
    }
    base.update(kwargs)
    return make_job(**base)


class TestRoleMatch:
    def test_good_job_passes(self, profile: Any) -> None:
        outcome = apply_hard_filters(good_job(), profile)

        assert isinstance(outcome, FilterOutcome)
        assert outcome.passed is True
        assert outcome.reasons == []

    def test_role_from_description_head(self, profile: Any) -> None:
        job = good_job(title="Infrastructure Specialist", description="SRE for a trading platform.")

        assert apply_hard_filters(job, profile).passed is True

    def test_unrelated_role_is_flagged_not_dropped(self, profile: Any) -> None:
        # ТЗ-эскалация: хард-фильтр по роли слишком агрессивен (883 из 898),
        # поэтому несовпадение теперь только флаг + штраф в скоринге.
        job = good_job(title="Sales Manager", description="Sells everything but engineering.")

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is True
        assert "роль не из include_keywords" in outcome.flags

    def test_unrelated_role_is_dropped_when_role_strict(self, profile: Any) -> None:
        profile.filters.role_strict = True
        job = good_job(title="Sales Manager", description="Sells everything but engineering.")

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is False
        assert "роль не из include_keywords" in outcome.reasons
        assert "роль не из include_keywords" not in outcome.flags

    def test_role_is_scanned_deep_into_the_description(self, profile: Any) -> None:
        profile.filters.role_scan_chars = 1200
        job = good_job(
            title="Technical Specialist",
            description=("x" * 900) + " part of the platform engineer team",
        )

        assert "роль не из include_keywords" not in apply_hard_filters(job, profile).flags

    def test_not_engineering_role_is_flagged_not_dropped(self, profile: Any) -> None:
        job = good_job(title="DevOps", description="platform engineer role")

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is True
        assert "роль не похожа на инженерную" not in outcome.flags

    def test_non_engineering_title_without_keyword(self, profile: Any) -> None:
        # «devops» in the description keeps the vacancy, but the title is flagged.
        job = good_job(title="Team Captain", description="devops practice, kubernetes")

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is True
        assert outcome.flags == ["роль не похожа на инженерную"]


class TestRemoteOnly:
    def test_onsite_is_rejected(self, profile: Any) -> None:
        job = good_job(remote=False)

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is False
        assert "не remote" in outcome.reasons

    def test_unknown_remote_is_not_rejected(self, profile: Any) -> None:
        assert apply_hard_filters(good_job(remote=None), profile).passed is True

    def test_geo_restriction_is_a_flag_not_a_drop(self, profile: Any) -> None:
        job = good_job(geo_restricted=True)

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is True
        assert outcome.flags == ["гео-ограничение"]


class TestExcludeKeywords:
    def test_exclude_in_title(self, profile: Any) -> None:
        job = good_job(title="Junior DevOps Engineer")

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is False
        assert any("junior" in r for r in outcome.reasons)

    def test_exclude_in_body_head_for_weak_tokens(self, profile: Any) -> None:
        job = good_job(title="DevOps Engineer", description="Level: intern, please apply.")

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is False
        assert any("intern" in r for r in outcome.reasons)

    def test_learned_stopword(self, profile: Any) -> None:
        job = good_job(title="Senior Blockchain DevOps")

        outcome = apply_hard_filters(job, profile, extra_stopwords=["blockchain"])

        assert outcome.passed is False
        assert any("blockchain" in r for r in outcome.reasons)


class TestCompanyBlocklist:
    def test_static_blocklist_from_profile(self, profile: Any) -> None:
        job = good_job(company="Acme Corp")

        outcome = apply_hard_filters(job, profile)

        assert outcome.passed is False
        assert any("Acme" in r for r in outcome.reasons)

    def test_dynamic_blocklist_from_bot(self, profile: Any) -> None:
        job = good_job(company="Binance")

        outcome = apply_hard_filters(job, profile, blocklist=["binance"])

        assert outcome.passed is False
        assert any("в блоке" in r for r in outcome.reasons)

    def test_blocklist_is_case_insensitive(self, profile: Any) -> None:
        job = good_job(company="CRYPTOCO")

        outcome = apply_hard_filters(job, profile, blocklist=["crypto"])

        assert outcome.passed is False


class TestSanctionsWarning:
    @pytest.mark.parametrize(
        ("company", "expected"),
        [("Acme Inc", "⚠️ санкционный риск: Acme"), ("Other Co", None)],
    )
    def test_warning_or_none(self, company: str, expected: str | None) -> None:
        assert sanctions_warning(good_job(company=company), ["Acme"]) == expected

    def test_warning_does_not_affect_filters(self, profile: Any) -> None:
        job = good_job(company="Acme")

        assert apply_hard_filters(job, profile).passed is False
