"""Offline tests for ``src/storage`` — idempotent upserts and deduplication keys."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.models import (
    AtsProvider,
    Contact,
    ContactSourceMethod,
    FundingStage,
    Opportunity,
    Startup,
    WorkMode,
)
from src.storage import Repository
from src.storage.repository import STARTUPS


def _startup(**overrides: object) -> Startup:
    data: dict = {"name": "ExampleLabs", "canonical_domain": "https://www.examplelabs.in/careers"}
    return Startup.model_validate(data | overrides)


def _opportunity(startup_id: int | None, **overrides: object) -> Opportunity:
    data: dict = {
        "startup_id": startup_id,
        "source": "greenhouse",
        "title": "Backend Engineering Intern",
        "url": "https://boards.greenhouse.io/examplelabs/jobs/12345",
        "city_canonical": "hyderabad",
        "work_mode": "onsite",
    }
    return Opportunity.model_validate(data | overrides)


def _contact(startup_id: int, **overrides: object) -> Contact:
    data: dict = {
        "startup_id": startup_id,
        "email": "careers@examplelabs.in",
        "source_method": "direct_public",
        "confidence_score": 0.95,
    }
    return Contact.model_validate(data | overrides)


# ---------------------------------------------------------------- schema / lifecycle
def test_schema_creation_is_idempotent_and_data_persists(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "dir" / "scraper.db"  # parent dirs auto-created
    with Repository.open(db) as repo:
        repo.upsert_startup(_startup())
    with Repository.open(db) as repo:  # reopen: create_schema again, data still there
        repo.create_schema()
        assert repo.counts() == {"startups": 1, "opportunities": 0, "contacts": 0, "leads": 0}


def test_in_memory_database_shares_state() -> None:
    with Repository.open(":memory:") as repo:
        sid = repo.upsert_startup(_startup())
        assert repo.get_startup(sid) is not None


# ---------------------------------------------------------------- startups
@pytest.mark.parametrize(
    "domain",
    [
        "examplelabs.in",
        "https://ExampleLabs.in",
        "http://www.examplelabs.in/about?x=1",
        "WWW.examplelabs.IN:443",
    ],
)
def test_startup_dedupes_on_canonical_domain(repo: Repository, domain: str) -> None:
    first = repo.upsert_startup(_startup())
    again = repo.upsert_startup(_startup(canonical_domain=domain))
    assert first == again
    assert repo.counts()["startups"] == 1


def test_startup_merge_never_erases_known_values(repo: Repository) -> None:
    sid = repo.upsert_startup(
        _startup(
            hq_city_canonical="hyderabad",
            hq_state="Telangana",
            hq_lat=17.385,
            hq_lng=78.4867,
            tech_stack=["Python", "React"],
            ats_provider="greenhouse",
            ats_slug="examplelabs",
            open_to_cold_pitch=True,
        )
    )
    # A later, less-informed sighting (e.g. from an RSS feed) must not wipe enrichment.
    repo.upsert_startup(_startup(name="ExampleLabs Pvt Ltd", funding_stage="seed"))
    stored = repo.get_startup(sid)
    assert stored is not None
    assert stored.name == "ExampleLabs Pvt Ltd"  # new non-null value wins
    assert stored.funding_stage is FundingStage.SEED
    assert stored.hq_city_canonical == "hyderabad"  # "unknown" sentinel did not overwrite
    assert stored.hq_state == "Telangana"
    assert stored.tech_stack == ["python", "react"]  # [] did not overwrite
    assert stored.ats_provider is AtsProvider.GREENHOUSE
    assert stored.open_to_cold_pitch is True


def test_startup_roundtrip_types(repo: Repository) -> None:
    checked = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)
    sid = repo.upsert_startup(
        _startup(last_funded_at=date(2026, 10, 1), last_checked_at=checked, hidden_score=72)
    )
    stored = repo.get_startup_by_domain("https://examplelabs.in/team")
    assert stored is not None
    assert stored.id == sid
    assert stored.last_funded_at == date(2026, 10, 1)
    assert stored.last_checked_at == checked
    assert stored.last_checked_at.tzinfo is not None
    assert stored.hidden_score == 72


def test_startup_created_at_is_insert_only(repo: Repository) -> None:
    repo.upsert_startup(_startup())
    with repo.engine.connect() as conn:
        created_1, updated_1 = conn.execute(
            select(STARTUPS.c.created_at, STARTUPS.c.updated_at)
        ).one()
    repo.upsert_startup(_startup(team_size=12))
    with repo.engine.connect() as conn:
        created_2, updated_2 = conn.execute(
            select(STARTUPS.c.created_at, STARTUPS.c.updated_at)
        ).one()
    assert created_2 == created_1
    assert updated_2 >= updated_1


def test_missing_startup_returns_none(repo: Repository) -> None:
    assert repo.get_startup(999) is None
    assert repo.get_startup_by_domain("nope.in") is None


# ---------------------------------------------------------------- opportunities
def test_opportunity_dedupes_on_normalized_title_and_url(repo: Repository) -> None:
    sid = repo.upsert_startup(_startup())
    first = repo.upsert_opportunity(_opportunity(sid))
    again = repo.upsert_opportunity(
        _opportunity(
            sid,
            title="  backend   ENGINEERING intern ",
            url="https://boards.greenhouse.io/examplelabs/jobs/12345/?utm_source=linkedin#apply",
        )
    )
    assert first == again
    assert repo.counts()["opportunities"] == 1


def test_different_posting_url_is_a_new_opportunity(repo: Repository) -> None:
    sid = repo.upsert_startup(_startup())
    a = repo.upsert_opportunity(_opportunity(sid))
    b = repo.upsert_opportunity(
        _opportunity(sid, url="https://boards.greenhouse.io/examplelabs/jobs/99999")
    )
    assert a != b
    assert len(repo.list_opportunities(startup_id=sid)) == 2


def test_opportunity_rescrape_preserves_first_seen_and_merges(repo: Repository) -> None:
    sid = repo.upsert_startup(_startup())
    oid = repo.upsert_opportunity(_opportunity(sid, stipend_inr_min=20000, has_ppo=True))
    before = repo.get_opportunity(oid)
    repo.upsert_opportunity(_opportunity(sid, duration_months=6))
    after = repo.get_opportunity(oid)
    assert before is not None
    assert after is not None
    assert after.first_seen_at == before.first_seen_at
    assert after.last_seen_at is not None
    assert before.last_seen_at is not None
    assert after.last_seen_at >= before.last_seen_at
    assert after.stipend_inr_min == 20000
    assert after.is_paid is True
    assert after.has_ppo is True
    assert after.duration_months == 6
    assert after.work_mode is WorkMode.ONSITE


def test_opportunity_without_startup_uses_url_domain(repo: Repository) -> None:
    oid = repo.upsert_opportunity(
        _opportunity(None, source="hn_hiring", url="https://news.ycombinator.com/item?id=1")
    )
    assert (
        repo.upsert_opportunity(
            _opportunity(None, source="hn_hiring", url="https://news.ycombinator.com/item?id=1")
        )
        == oid
    )


def test_opportunity_for_missing_startup_raises(repo: Repository) -> None:
    with pytest.raises(LookupError, match="does not exist"):
        repo.upsert_opportunity(_opportunity(42))


def test_batch_opportunity_upsert_is_idempotent(repo: Repository) -> None:
    sid = repo.upsert_startup(_startup())
    batch = [_opportunity(sid, url=f"https://jobs.lever.co/examplelabs/{i}") for i in range(5)]
    ids_1 = repo.upsert_opportunities(batch)
    ids_2 = repo.upsert_opportunities(batch)
    assert ids_1 == ids_2
    assert len(set(ids_1)) == 5
    assert repo.counts()["opportunities"] == 5


# ---------------------------------------------------------------- contacts
def test_contact_email_dedupe_is_case_insensitive(repo: Repository) -> None:
    sid = repo.upsert_startup(_startup())
    a = repo.upsert_contact(_contact(sid))
    b = repo.upsert_contact(_contact(sid, email="mailto:CAREERS@ExampleLabs.in"))
    assert a == b
    assert repo.counts()["contacts"] == 1


def test_contact_profile_url_dedupe(repo: Repository) -> None:
    sid = repo.upsert_startup(_startup())
    kwargs = {"email": None, "source_method": "github_public", "confidence_score": 0.85}
    a = repo.upsert_contact(_contact(sid, profile_url="https://github.com/example-dev", **kwargs))
    b = repo.upsert_contact(_contact(sid, profile_url="https://github.com/example-dev/", **kwargs))
    assert a == b


def test_same_email_at_two_startups_is_two_contacts(repo: Repository) -> None:
    s1 = repo.upsert_startup(_startup())
    s2 = repo.upsert_startup(_startup(name="Other", canonical_domain="other.in"))
    assert repo.upsert_contact(_contact(s1)) != repo.upsert_contact(_contact(s2))


def test_contact_keeps_most_confident_method_and_sticky_mx(repo: Repository) -> None:
    sid = repo.upsert_startup(_startup())
    repo.upsert_contact(
        _contact(sid, source_method="pattern_mx_verified", confidence_score=0.5, verified_mx=True)
    )
    repo.upsert_contact(_contact(sid, source_method="direct_public", confidence_score=0.95))
    repo.upsert_contact(_contact(sid, source_method="github_public", confidence_score=0.85))
    (stored,) = repo.list_contacts(startup_id=sid)
    assert stored.source_method is ContactSourceMethod.DIRECT_PUBLIC
    assert stored.confidence_score == 0.95
    assert stored.verified_mx is True


def test_contact_requires_existing_startup(repo: Repository) -> None:
    with pytest.raises(IntegrityError):  # PRAGMA foreign_keys=ON is active
        repo.upsert_contact(_contact(12345))
