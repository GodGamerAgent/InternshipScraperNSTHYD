"""Offline tests for ``src/enrichment/stipend.py`` (Indian internship conventions)."""

import pytest

from src.enrichment.stipend import (
    Stipend,
    detect_ppo,
    extract_internship_terms,
    parse_duration_months,
    parse_stipend,
    stipend_from_structured,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Stipend: ₹15,000/month", (15000, 15000, True)),
        ("INR 20,000 per month", (20000, 20000, True)),
        ("Rs. 25000 pm", (25000, 25000, True)),
        ("Rs 18,000 p.m.", (18000, 18000, True)),
        ("15k - 25k/mo", (15000, 25000, True)),
        ("₹15K–25K per month", (15000, 25000, True)),
        ("15-25k per month", (15000, 25000, True)),
        ("₹10,000 to ₹12,500 monthly", (10000, 12500, True)),
        ("Stipend: 30,000", (30000, 30000, True)),
        ("We offer ₹35,000 stipend", (35000, 35000, True)),
        ("Stipend of up to ₹40,000 per month", (None, 40000, True)),
        ("1 lakh/month for exceptional candidates", (100000, 100000, True)),
        ("₹1,00,000 per month", (100000, 100000, True)),  # Indian digit grouping
        ("PPO CTC 6-8 LPA, stipend 20k per month", (20000, 20000, True)),
        ("Duration: 6 months. Stipend: ₹12,000", (12000, 12000, True)),
    ],
)
def test_parse_stipend_amounts(text: str, expected: tuple) -> None:
    s = parse_stipend(text)
    assert (s.min_inr, s.max_inr, s.is_paid) == expected


@pytest.mark.parametrize(
    "text",
    [
        "PPO with 12 LPA CTC",  # annual package, not a stipend
        "₹6,00,000 per annum",
        "$2000/month",  # foreign currency
        "2000 USD per month",
        "Stipend: MYR 2,000",  # regression: foreign code after the word "stipend"
        "Stipend: S$1,500 per month",
        "Stipend: 1500 SGD/month",
        "Raised ₹500 Cr in Series B",
        "10,000+ customers across India",
        "Stipend: 20",  # below the sanity floor
        "",
        None,
    ],
)
def test_parse_stipend_ignores_non_stipends(text: str | None) -> None:
    assert not parse_stipend(text).has_amount


@pytest.mark.parametrize(
    ("text", "is_paid"),
    [
        ("This is an unpaid internship", False),
        ("No stipend is offered", False),
        ("Stipend: N/A", False),
        ("Competitive stipend", True),
        ("Paid internship with mentorship", True),
        ("Performance-based stipend", True),
        ("Great team and snacks", None),
    ],
)
def test_parse_stipend_paid_flags(text: str, is_paid: bool | None) -> None:
    s = parse_stipend(text)
    assert s.is_paid is is_paid
    assert not s.has_amount


def test_amount_next_to_stipend_word_wins() -> None:
    s = parse_stipend("Laptop worth ₹80,000 provided. Stipend: ₹25,000 per month.")
    assert (s.min_inr, s.max_inr) == (25000, 25000)


@pytest.mark.parametrize(
    ("minimum", "maximum", "currency", "interval", "expected"),
    [
        (25000, 30000, "INR", "per-month-salary", Stipend(25000, 30000, True)),  # Lever
        (30000, 40000, "INR", "1 MONTH", Stipend(30000, 40000, True)),  # Ashby
        (30000, None, "inr", "1 MONTH", Stipend(30000, 30000, True)),
        (0, 0, "INR", "per-month-salary", None),  # Lever "undisclosed"
        (600000, 800000, "INR", "per-year-salary", None),
        (8000, 9000, "USD", "1 MONTH", None),
        (None, None, None, None, None),
    ],
)
def test_stipend_from_structured(minimum, maximum, currency, interval, expected) -> None:
    assert stipend_from_structured(minimum, maximum, currency, interval) == expected


@pytest.mark.parametrize(
    ("text", "months"),
    [
        ("Duration: 6 months", 6.0),
        ("6-month internship", 6.0),
        ("6 month internship", 6.0),
        ("3 to 6 months", 6.0),
        ("2-3 months", 3.0),
        ("12 weeks", 2.8),
        ("Internship duration - 2 months", 2.0),
        ("Experience: 6 months. Internship duration: 3 months", 3.0),
        ("0-6 months of experience required", None),
        ("Notice period: 2 months", None),
        ("We grew 3x within 6 months", None),
        ("Summer internship", None),
        ("48 months", None),  # outside sanity bounds
        (None, None),
    ],
)
def test_parse_duration_months(text: str | None, months: float | None) -> None:
    assert parse_duration_months(text) == months


@pytest.mark.parametrize(
    ("text", "ppo"),
    [
        ("PPO based on performance", True),
        ("Interns may receive a pre-placement offer", True),
        ("Pre placement offers for top performers", True),
        ("Opportunity for conversion to full-time", True),
        ("Full-time conversion possible", True),
        ("Please note: no PPO for this cohort", False),
        ("This internship does not include a PPO", False),
        ("Great team", None),
        (None, None),
    ],
)
def test_detect_ppo(text: str | None, ppo: bool | None) -> None:
    assert detect_ppo(text) is ppo


def test_extract_internship_terms_combines_all() -> None:
    terms = extract_internship_terms(
        "SDE Intern (6 months, PPO)\nStipend: 15k - 25k/mo. 0-1 years of experience."
    )
    assert terms.stipend == Stipend(15000, 25000, True)
    assert terms.duration_months == 6.0
    assert terms.has_ppo is True
