"""Offline tests for data contracts and the URL/text helpers they depend on."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.core.text import normalize_text
from src.core.urls import canonical_domain, normalize_url
from src.models import (
    CITY_UNKNOWN,
    Contact,
    Opportunity,
    ResolvedLocation,
    Startup,
    WorkMode,
    opportunity_fingerprint,
)


# ---------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.acme.in/careers", "acme.in"),
        ("HTTP://WWW2.Acme.co.in:8080/x", "acme.co.in"),
        ("acme.io", "acme.io"),
        ("careers.acme.in", "careers.acme.in"),  # subdomains are kept (see docstring)
        ("https://user:pw@acme.in.", "acme.in"),
    ],
)
def test_canonical_domain(value: str, expected: str) -> None:
    assert canonical_domain(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "localhost", "https://", "not a url"])
def test_canonical_domain_rejects_junk(value: str) -> None:
    with pytest.raises(ValueError, match=r"URL or domain|hostname"):
        canonical_domain(value)


def test_normalize_url_strips_tracking_and_noise() -> None:
    assert (
        normalize_url("http://www.Jobs.Lever.co/acme/1/?utm_source=x&b=2&a=1&gclid=z#apply")
        == "https://jobs.lever.co/acme/1?a=1&b=2"
    )


def test_normalize_text() -> None:
    assert normalize_text("Hi-Tech City, HYD (Remote/IST)") == "hi tech city hyd remote ist"
    assert normalize_text(None) == ""


# ---------------------------------------------------------------- Startup
def test_startup_from_url_and_tag_cleanup() -> None:
    s = Startup.from_url(
        "Acme", "https://www.acme.in/about", tech_stack=["Python", " python", "Go"]
    )
    assert s.canonical_domain == "acme.in"
    assert s.tech_stack == ["python", "go"]
    assert s.hq_city_canonical == CITY_UNKNOWN


def test_startup_validation_errors() -> None:
    with pytest.raises(ValidationError):
        Startup(name="A", canonical_domain="a.in", ats_slug="x")  # slug without provider
    with pytest.raises(ValidationError):
        Startup(name="A", canonical_domain="a.in", hq_lat=12.9)  # lat without lng
    with pytest.raises(ValidationError):
        Startup(name="A", canonical_domain="a.in", hidden_score=101)
    with pytest.raises(ValidationError):
        Startup(name="A", canonical_domain="a.in", unexpected_field=1)  # extra="forbid"


def test_startup_with_hq_copies_location() -> None:
    loc = ResolvedLocation(
        raw_location="Koramangala", country="IN", city_canonical="bengaluru", state="Karnataka",
        work_mode="onsite", lat=12.97, lng=77.59,
    )  # fmt: skip
    s = Startup(name="A", canonical_domain="a.in").with_hq(loc)
    assert (s.hq_city_canonical, s.hq_state, s.hq_lat) == ("bengaluru", "Karnataka", 12.97)


# ---------------------------------------------------------------- Opportunity
def _opp(**kw: object) -> Opportunity:
    base: dict = {"source": "lever", "title": "SDE Intern", "url": "https://jobs.lever.co/a/1"}
    return Opportunity.model_validate(base | kw)


def test_opportunity_infers_paid_from_stipend() -> None:
    assert _opp(stipend_inr_min=15000, stipend_inr_max=25000).is_paid is True
    assert _opp().is_paid is None
    assert _opp(is_paid=False).is_paid is False


@pytest.mark.parametrize(
    "kw",
    [
        {"stipend_inr_min": 30000, "stipend_inr_max": 20000},
        {"stipend_inr_min": -1},
        {"is_paid": False, "stipend_inr_min": 10000},
        {"duration_months": 0},
        {"url": "ftp://example.com/x"},
        {"source": "Not A Slug"},
        {"city_canonical": "remote_india", "work_mode": "onsite"},
    ],
)
def test_opportunity_rejects_inconsistent_data(kw: dict) -> None:
    with pytest.raises(ValidationError):
        _opp(**kw)


def test_opportunity_naive_datetime_is_treated_as_utc() -> None:
    naive = datetime(2026, 11, 1, 10, 0)  # noqa: DTZ001 - deliberately naive
    ist = timezone(timedelta(hours=5, minutes=30))
    assert _opp(posted_at=naive).posted_at == naive.replace(tzinfo=UTC)
    assert _opp(posted_at=datetime(2026, 11, 1, 15, 30, tzinfo=ist)).posted_at == datetime(
        2026, 11, 1, 10, 0, tzinfo=UTC
    )


def test_opportunity_with_location_and_fingerprint() -> None:
    loc = ResolvedLocation(country="IN", city_canonical="remote_india", work_mode="remote_india")
    opp = _opp().with_location(loc)
    assert opp.work_mode is WorkMode.REMOTE_INDIA
    assert opp.fingerprint("a.in") == opportunity_fingerprint("A.IN", " sde  INTERN", opp.url + "/")
    assert opp.fingerprint("a.in") != opportunity_fingerprint("b.in", opp.title, opp.url)


# ---------------------------------------------------------------- Contact
def test_contact_requires_email_or_profile() -> None:
    with pytest.raises(ValidationError, match="email or a profile_url"):
        Contact(startup_id=1, source_method="direct_public", confidence_score=0.9)


def test_contact_rejects_unverified_pattern_guess() -> None:
    with pytest.raises(ValidationError, match="verified_mx"):
        Contact(
            startup_id=1, email="first@a.in", source_method="pattern_mx_verified",
            confidence_score=0.5,
        )  # fmt: skip


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "a b@c.in", "x@-bad-.in"])
def test_contact_rejects_bad_email(email: str) -> None:
    with pytest.raises(ValidationError):
        Contact(startup_id=1, email=email, source_method="direct_public", confidence_score=0.9)


def test_contact_dedupe_key() -> None:
    c = Contact(
        startup_id=1, email="mailto:Hiring@Acme.IN?subject=Intern", source_method="direct_public",
        confidence_score=0.95,
    )  # fmt: skip
    assert c.email == "hiring@acme.in"
    assert c.dedupe_key == "email:hiring@acme.in"
    assert c.discovered_at.tzinfo is not None
