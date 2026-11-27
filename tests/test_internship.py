"""Offline tests for the internship classifier driven by the real ``config/sources.yaml``."""

import pytest

from src.collectors.config import SourcesConfig
from src.enrichment.internship import InternshipMatcher
from tests.conftest import CONFIG_DIR


@pytest.fixture(scope="module")
def matcher() -> InternshipMatcher:
    return InternshipMatcher.from_config(SourcesConfig.load(CONFIG_DIR).internship)


@pytest.mark.parametrize(
    ("title", "employment_type", "expected"),
    [
        ("AI Researcher Intern", None, True),
        ("Software Development Engineer Intern", None, True),
        ("Marketing internship (brand & communication)", None, True),
        ("Summer Internship 2027 - Data", None, True),
        ("Product Manager Intern", None, True),  # "manager" is not an exclusion
        ("Lead Generation Intern", None, True),  # neither is "lead"
        ("SDE (6 months, PPO)", None, True),
        ("Payroll Industrial Trainee", None, True),
        ("People Partner", "Intern", True),  # Lever commitment
        ("Product Design", "Internship", True),  # Greenhouse department / Workable type
        ("Assistant Manager - Internal Audit", None, False),  # "internal" != "intern"
        ("International Sales Manager", None, False),
        ("Senior Engineer - mentor interns", None, False),
        ("Staff Engineer", "Intern", False),  # seniority beats a mislabeled type
        ("Contract Data Intern", None, False),
        ("Backend Engineer", "Full-time Employment", False),
        ("Backend Engineer", None, False),
    ],
)
def test_is_internship(
    matcher: InternshipMatcher, title: str, employment_type: str | None, expected: bool
) -> None:
    assert matcher.is_internship(title, employment_type=employment_type) is expected
