"""Dedup: одна вакансия из двух источников должна стать одной карточкой (ТЗ §6)."""

from __future__ import annotations

from typing import Any

from app.pipeline.dedup import dedupe
from tests.conftest import make_job


class TestDedupByContent:
    def test_same_content_different_sources_becomes_one(self) -> None:
        first = make_job(source="gh", sources=["gh"], url="https://a.io/jobs/1")
        second = make_job(source="remoteok", sources=["remoteok"], url="https://b.io/j/2")

        result = dedupe([first, second])

        assert len(result) == 1
        assert result[0] is first
        assert result[0].sources == ["gh", "remoteok"]

    def test_different_content_stays_separate(self) -> None:
        first = make_job(title="Senior DevOps Engineer")
        second = make_job(title="Staff SRE", url="https://b.io/j/2")

        assert len(dedupe([first, second])) == 2

    def test_order_is_preserved(self) -> None:
        jobs = [
            make_job(title="A DevOps Engineer", url="https://a.io/1"),
            make_job(title="B SRE", url="https://a.io/2"),
            make_job(title="A DevOps Engineer", url="https://a.io/3"),
        ]

        result = dedupe(jobs)

        assert [j.title for j in result] == ["A DevOps Engineer", "B SRE"]


class TestDedupByUrl:
    def test_same_canonical_url_different_content(self) -> None:
        first = make_job(company="Alpha", title="DevOps A", url="https://a.io/jobs/1")
        second = make_job(
            company="Beta",
            title="DevOps B",
            url="https://a.io/jobs/1?utm_source=telegram",
            source="wwr",
        )

        result = dedupe([first, second])

        assert len(result) == 1
        assert result[0].company == "Alpha"
        assert result[0].sources == ["wwr"]

    def test_empty_url_never_matches_on_url(self) -> None:
        first = make_job(title="DevOps A", url="")
        second = make_job(title="DevOps B", url="")

        assert len(dedupe([first, second])) == 2


class TestMerge:
    def test_fills_missing_fields_from_duplicate(self) -> None:
        # Same canonical URL, different content hash -> merged via the URL index.
        first = make_job(description="", salary_min=None, remote=None, location="")
        second = make_job(
            description="Kubernetes and Terraform",
            salary_min=4200.0,
            salary_max=4800.0,
            currency="USD",
            remote=True,
            location="Remote (EMEA)",
            url="https://a.io/jobs/1?utm_source=telegram",
        )

        result = dedupe([first, second])

        assert len(result) == 1
        merged = result[0]
        assert merged.description == "Kubernetes and Terraform"
        assert merged.salary_min == 4200.0
        assert merged.salary_max == 4800.0
        assert merged.currency == "USD"
        assert merged.remote is True
        assert merged.location == "Remote (EMEA)"

    def test_existing_values_win(self) -> None:
        first = make_job(description="primary text", salary_min=5000.0)
        second = make_job(description="other text", salary_min=1000.0, url="https://b.io/j/2")

        merged: Any = dedupe([first, second])[0]

        assert merged.description == "primary text"
        assert merged.salary_min == 5000.0

    def test_duplicate_sources_are_not_repeated(self) -> None:
        first = make_job(sources=["gh"])
        second = make_job(source="gh", sources=["gh"], url="https://b.io/j/2")

        assert dedupe([first, second])[0].sources == ["gh"]
