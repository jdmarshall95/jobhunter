"""Прозрачный скоринг 0–100 с разбором (ТЗ §6 «Scoring»)."""

from __future__ import annotations

from typing import Any

import pytest

from app.models import GeoScope, Job
from app.scoring.scorer import FxCache, ScoreResult, score_job
from tests.conftest import make_job

STRONG_DESCRIPTION = (
    "We are a crypto exchange looking for a senior DevOps engineer. "
    "Kubernetes, Terraform, Ansible, GitLab CI, Helm, Prometheus, Grafana, AWS. "
    "Russian-speaking team, fully remote."
)


def scored(job: Job, profile: Any, **kwargs: Any) -> ScoreResult:
    return score_job(job, profile, **kwargs)


class TestStrongMatch:
    def test_high_score_and_breakdown(self, profile: Any) -> None:
        job = make_job(description=STRONG_DESCRIPTION, salary_min=4000.0, currency="USD")

        result = scored(job, profile)

        assert job.score >= profile.min_score
        assert job.score == result.score
        assert job.score_breakdown == result.breakdown
        labels = [label for label, _ in result.breakdown]
        assert any("стек ядра" in label for label in labels)
        assert any("домен" in label for label in labels)
        assert any("русскоязычная команда" in label for label in labels)

    def test_score_is_clamped_to_hundred(self, profile: Any) -> None:
        job = make_job(description=STRONG_DESCRIPTION * 3, salary_min=9000.0, currency="USD")

        assert 0.0 <= scored(job, profile).score <= 100.0

    def test_empty_job_never_goes_negative(self, profile: Any) -> None:
        job = make_job(title="Sales Manager", description="", remote=False, salary_min=None)

        result = scored(job, profile)

        assert job.score == max(0.0, sum(d for _, d in result.breakdown))


class TestSalary:
    @pytest.mark.parametrize(
        ("salary", "currency", "label_part"),
        [
            (4500.0, "USD", "вилка ≥ цели"),
            (3500.0, "USD", "вилка в диапазоне"),
            (2000.0, "USD", "вилка ниже минимума"),
        ],
    )
    def test_salary_bands(
        self, profile: Any, salary: float, currency: str, label_part: str
    ) -> None:
        job = make_job(description="DevOps", salary_min=salary, currency=currency)

        labels = [label for label, _ in scored(job, profile).breakdown]

        assert any(label_part in label for label in labels)

    def test_unknown_salary_gets_small_bonus(self, profile: Any) -> None:
        job = make_job(description="DevOps", salary_min=None, salary_max=None)

        result = scored(job, profile)

        assert ("вилка не указана", 4.0) in result.breakdown

    def test_max_is_used_when_present(self, profile: Any) -> None:
        job = make_job(description="DevOps", salary_min=2000.0, salary_max=5000.0, currency="USD")

        labels = [label for label, _ in scored(job, profile).breakdown]

        assert any("вилка ≥ цели" in label for label in labels)

    def test_eur_is_converted(self, profile: Any) -> None:
        job = make_job(description="DevOps", salary_min=4000.0, currency="EUR")

        labels = [label for label, _ in scored(job, profile).breakdown]

        # 4000 EUR * 1.08 = 4320 USD -> above the $4k target.
        assert any("4,320" in label for label in labels)


class TestPenalties:
    def test_onsite_penalty(self, profile: Any) -> None:
        result = scored(make_job(description="DevOps", remote=False), profile)

        assert ("не remote", profile.scoring.penalty_onsite) in result.breakdown
        # The public score never drops below zero even when penalties dominate.
        assert result.score == max(0.0, sum(d for _, d in result.breakdown))

    def test_junior_penalty(self, profile: Any) -> None:
        result = scored(
            make_job(title="Junior DevOps Engineer", description="junior role"), profile
        )

        assert ("грейд junior", -profile.scoring.seniority_match) in result.breakdown

    def test_scam_marks_job_suspicious(self, profile: Any) -> None:
        job = make_job(description="DevOps with unpaid internship, own wallet required")

        result = scored(job, profile)

        assert job.suspicious is True
        assert "suspicious" in result.flags
        assert ("подозрение на скам", profile.scoring.penalty_scam_suspect) in result.breakdown

    def test_residency_penalty(self, profile: Any) -> None:
        result = scored(make_job(description="DevOps, work permit required"), profile)

        assert (
            "нужно резидентство/разрешение",
            profile.scoring.penalty_residency,
        ) in result.breakdown

    def test_missing_ru_signal_flag(self, profile: Any) -> None:
        result = scored(make_job(description="DevOps in a London bank"), profile)

        assert "RU-сигнал не найден" in result.flags

    @pytest.mark.parametrize(
        "description",
        [
            "DevOps engineer, high precision pipeline, decisions owned by the team.",
            "You will need concise communication and an incision-free workflow.",
            "DevOps role, English is a plus.",
        ],
    )
    def test_ru_signal_is_not_a_substring_match(self, profile: Any, description: str) -> None:
        """«cis» inside *precision* used to hand out the +8 bonus to everyone."""
        result = scored(make_job(description=description), profile)

        labels = [label for label, _ in result.breakdown]
        assert not any("русскоязычная команда" in label for label in labels)
        assert "RU-сигнал не найден" in result.flags

    @pytest.mark.parametrize(
        "description",
        [
            "DevOps in a russian speaking team, fully remote.",
            "Работа в русскоязычной команде.",
            "We are a CIS-based fintech.",
        ],
    )
    def test_ru_signal_recognises_real_mentions(self, profile: Any, description: str) -> None:
        result = scored(make_job(description=description), profile)

        labels = [label for label, _ in result.breakdown]
        assert any("русскоязычная команда" in label for label in labels)

    def test_repeated_reject_feedback(self, profile: Any) -> None:
        job = make_job(description="DevOps for a blockchain exchange")

        result = scored(job, profile, negative_signals={"blockchain": 3})

        assert ("ты часто отклонял «blockchain»", -4.0) in result.breakdown

    def test_single_reject_is_not_enough(self, profile: Any) -> None:
        job = make_job(description="DevOps for a blockchain exchange")

        result = scored(job, profile, negative_signals={"blockchain": 1})

        assert all("отклонял" not in label for label, _ in result.breakdown)

    def test_usa_only_takes_the_heavy_penalty(self, profile: Any) -> None:
        job = make_job(description="DevOps", location="Remote - USA", geo_scope=GeoScope.USA_ONLY)

        result = scored(job, profile)

        assert ("гео: только USA", profile.scoring.penalty_usa_only) in result.breakdown

    def test_city_scoped_remote_is_penalised_lightly(self, profile: Any) -> None:
        job = make_job(
            description="DevOps",
            location="Remote - Singapore",
            geo_scope=GeoScope.COUNTRY,
            geo_place="Singapore",
        )

        result = scored(job, profile)

        assert ("не worldwide remote (Singapore)", profile.scoring.penalty_not_worldwide) in (
            result.breakdown
        )

    def test_worldwide_remote_has_no_geo_penalty(self, profile: Any) -> None:
        job = make_job(description="DevOps", location="Remote", geo_scope=GeoScope.WORLDWIDE)

        labels = [label for label, _ in scored(job, profile).breakdown]

        assert not any("гео" in label or "worldwide" in label for label in labels)

    def test_europe_scope_has_no_geo_penalty(self, profile: Any) -> None:
        job = make_job(
            description="DevOps", location="Remote - Warsaw, Poland", geo_scope=GeoScope.EUROPE
        )

        labels = [label for label, _ in scored(job, profile).breakdown]

        assert not any("гео" in label or "worldwide" in label for label in labels)


class TestFxCache:
    def test_usd_is_identity(self) -> None:
        assert FxCache().to_usd(4000.0, "USD") == 4000.0

    @pytest.mark.parametrize("currency", ["USDT", "USDC"])
    def test_stablecoins_are_one_to_one(self, currency: str) -> None:
        assert FxCache().to_usd(4000.0, currency) == 4000.0

    def test_unknown_currency_defaults_to_one(self) -> None:
        assert FxCache().to_usd(4000.0, "XYZ") == 4000.0

    def test_custom_rates_override(self) -> None:
        assert FxCache({"EUR": 1.20}).to_usd(1000.0, "eur") == 1200.0


class TestScoreResultText:
    def test_to_text_respects_limit(self) -> None:
        result = ScoreResult(50.0, [("a", 10.0), ("b", -5.0), ("c", 3.0)], [])

        assert result.to_text(limit=2) == "a +10; b -5"
