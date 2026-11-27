"""Offline tests for repository filters, closing vanished postings, and stats."""

from datetime import UTC, datetime

import pytest

from src.models import Contact, Opportunity, OpportunityStatus, Startup, StartupLead, WorkMode
from src.storage import ExportFilter, Repository


def opp(sid: int | None, n: int, city: str, mode: str, source: str = "greenhouse") -> Opportunity:
    return Opportunity(
        startup_id=sid,
        source=source,
        title=f"Intern {n}",
        url=f"https://boards.example/{source}/{n}",
        city_canonical=city,
        work_mode=mode,
        posted_at=datetime(2026, 11, n, tzinfo=UTC),
    )


@pytest.fixture
def filled(repo: Repository) -> dict[str, int]:
    ids = {
        "hyd": repo.upsert_startup(
            Startup(name="HydCo", canonical_domain="hyd.example", hq_city_canonical="hyderabad",
                    hidden_score=80)
        ),
        "blr": repo.upsert_startup(
            Startup(name="BlrCo", canonical_domain="blr.example", hq_city_canonical="bengaluru",
                    hidden_score=40)
        ),
        "pune": repo.upsert_startup(
            Startup(name="PuneCo", canonical_domain="pune.example", hq_city_canonical="pune")
        ),
    }  # fmt: skip
    repo.upsert_opportunities(
        [
            opp(ids["hyd"], 1, "hyderabad", "onsite"),
            opp(ids["hyd"], 2, "hyderabad", "hybrid"),
            opp(ids["blr"], 3, "bengaluru", "onsite"),
            opp(ids["blr"], 4, "remote_india", "remote_india"),
            opp(ids["pune"], 5, "pune", "onsite"),
            opp(None, 6, "unknown", "remote_global", source="hn_hiring"),
        ]
    )
    repo.upsert_contact(
        Contact(startup_id=ids["hyd"], email="careers@hyd.example", source_method="direct_public",
                confidence_score=0.95)
    )  # fmt: skip
    repo.upsert_contact(
        Contact(startup_id=ids["blr"], email="jobs@blr.example", source_method="direct_public",
                confidence_score=0.95)
    )  # fmt: skip
    repo.upsert_lead(StartupLead(name="HydCo", source="thub_lab32", domain="hyd.example",
                                 startup_id=ids["hyd"], hq_city_canonical="hyderabad"))  # fmt: skip
    repo.upsert_lead(
        StartupLead(name="NoSite Labs", source="entrackr", hq_city_canonical="chennai")
    )
    return ids


def titles(rows) -> list[str]:
    return [o.title for o, _ in rows]


def test_city_filter_and_include_remote(repo: Repository, filled: dict[str, int]) -> None:
    hyd = repo.query_opportunities(ExportFilter(cities=frozenset({"hyderabad"})))
    assert titles(hyd) == ["Intern 2", "Intern 1"]  # newest first within the same score
    with_remote = repo.query_opportunities(
        ExportFilter(cities=frozenset({"hyderabad"}), include_remote=True)
    )
    assert set(titles(with_remote)) == {"Intern 1", "Intern 2", "Intern 4"}


def test_work_mode_and_min_score(repo: Repository, filled: dict[str, int]) -> None:
    onsite = ExportFilter(work_modes=frozenset({WorkMode.ONSITE}))
    assert set(titles(repo.query_opportunities(onsite))) == {"Intern 1", "Intern 3", "Intern 5"}
    scored = repo.query_opportunities(ExportFilter(min_hidden_score=60))
    assert titles(scored) == ["Intern 2", "Intern 1"]  # unscored startups and HN excluded
    every = repo.query_opportunities(ExportFilter())
    assert len(every) == 6
    assert every[0][1] is not None
    assert every[0][1].name == "HydCo"  # best score first
    # unscored last (PuneCo + the startup-less HN post), newest first among them
    assert titles(every[-2:]) == ["Intern 6", "Intern 5"]
    assert every[-2][1] is None


def test_closing_and_status_filter(repo: Repository, filled: dict[str, int]) -> None:
    kept = [o.id for o, _ in repo.query_opportunities(ExportFilter()) if o.title == "Intern 1"]
    closed = repo.close_missing_opportunities(
        source="greenhouse", startup_domain="hyd.example", keep_ids=kept
    )
    assert closed == 1  # Intern 2 vanished from HydCo's board; other companies untouched
    assert "Intern 2" not in titles(repo.query_opportunities(ExportFilter()))
    closed_rows = repo.query_opportunities(ExportFilter(status=OpportunityStatus.CLOSED))
    assert titles(closed_rows) == ["Intern 2"]
    assert len(repo.query_opportunities(ExportFilter(status=None))) == 6

    # whole-source scope (HN) and unknown company
    assert (
        repo.close_missing_opportunities(source="hn_hiring", startup_domain=None, keep_ids=[]) == 1
    )
    assert repo.close_missing_opportunities(source="greenhouse", startup_domain="nope.example",
                                            keep_ids=[]) == 0  # fmt: skip


def test_reappearing_posting_is_reopened(repo: Repository, filled: dict[str, int]) -> None:
    repo.close_missing_opportunities(source="greenhouse", startup_domain="hyd.example", keep_ids=[])
    repo.upsert_opportunity(opp(filled["hyd"], 1, "hyderabad", "onsite"))
    assert "Intern 1" in titles(repo.query_opportunities(ExportFilter()))


def test_startup_contact_lead_queries(repo: Repository, filled: dict[str, int]) -> None:
    names = [s.name for s in repo.query_startups(ExportFilter())]
    assert names == ["HydCo", "BlrCo", "PuneCo"]  # scored first, unscored last
    remote = repo.query_startups(ExportFilter(cities=frozenset({"pune"}), include_remote=True))
    assert {s.name for s in remote} == {"PuneCo", "BlrCo"}  # BlrCo has a Remote-India role
    hybrid = repo.query_startups(ExportFilter(work_modes=frozenset({WorkMode.HYBRID})))
    assert [s.name for s in hybrid] == ["HydCo"]

    contacts = repo.query_contacts(ExportFilter(min_hidden_score=60))
    assert [(c.email, s.name) for c, s in contacts] == [("careers@hyd.example", "HydCo")]

    assert {
        lead.name for lead in repo.query_leads(ExportFilter(cities=frozenset({"chennai"})))
    } == {"NoSite Labs"}
    assert [lead.name for lead in repo.query_leads(ExportFilter(min_hidden_score=60))] == ["HydCo"]


def test_stats_and_counts(repo: Repository, filled: dict[str, int]) -> None:
    stats = repo.stats()
    assert stats["counts"] == {"startups": 3, "opportunities": 6, "contacts": 2, "leads": 2}
    assert stats["opportunities_by_status"] == {"open": 6}
    assert stats["open_by_city"]["hyderabad"] == 2
    assert stats["open_by_city"]["unknown"] == 1  # NULL city shown as "unknown"
    assert stats["open_by_source"] == {"greenhouse": 5, "hn_hiring": 1}
    assert sorted(s for s in stats["startup_scores"] if s is not None) == [40, 80]
    assert stats["leads_with_domain"] == 1
    assert repo.open_opportunity_counts() == {filled["hyd"]: 2, filled["blr"]: 2, filled["pune"]: 1}
