"""Offline tests for export rendering, stats summary, and stale-posting closure in the pipeline."""

import csv
import io
import json
from datetime import UTC, date, datetime

from src.core.base import CollectorResult
from src.enrichment.scorer import ScoringConfig
from src.exports import COLUMNS, build_rows, default_export_path, render, summarize_stats
from src.models import CollectedOpportunity, Opportunity, OpportunityStatus, Startup
from src.pipeline import persist_results
from src.storage import ExportFilter, Repository
from tests.conftest import CONFIG_DIR
from tests.test_queries import filled

__all__ = ["filled"]


def test_csv_has_fixed_header_and_neutralizes_formulas() -> None:
    rows = [
        {
            "title": '=HYPERLINK("http://evil.example")',
            "company": "+Acme",
            "city": "hyderabad",
            "is_paid": True,
            "has_ppo": None,
            "posted_at": datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
        }
    ]
    text = render(rows, "opportunities", "csv")
    header, row = list(csv.reader(io.StringIO(text)))
    assert tuple(header) == COLUMNS["opportunities"]
    record = dict(zip(header, row, strict=True))
    assert record["title"].startswith("'=")  # not executable in a spreadsheet
    assert record["company"] == "'+Acme"
    assert record["is_paid"] == "yes"
    assert record["has_ppo"] == ""
    assert record["posted_at"] == "2026-11-01T04:30:00+00:00"
    assert render([], "leads", "csv").strip() == ",".join(COLUMNS["leads"])


def test_json_keeps_types(repo: Repository, filled: dict[str, int]) -> None:
    rows = build_rows(repo, "startups", ExportFilter())
    payload = json.loads(render(rows, "startups", "json"))
    first = payload[0]
    assert list(first) == list(COLUMNS["startups"])
    assert (first["name"], first["hidden_score"], first["open_internships"]) == ("HydCo", 80, 2)
    assert isinstance(first["tech_stack"], list)


def test_build_rows_for_every_kind(repo: Repository, filled: dict[str, int]) -> None:
    flt = ExportFilter(cities=frozenset({"hyderabad"}), include_remote=True)
    opportunities = build_rows(repo, "opportunities", flt)
    assert {r["title"] for r in opportunities} == {"Intern 1", "Intern 2", "Intern 4"}
    assert all(set(r) == set(COLUMNS["opportunities"]) for r in opportunities)
    contacts = build_rows(repo, "contacts", ExportFilter(min_hidden_score=60))
    assert [(r["company"], r["email"]) for r in contacts] == [("HydCo", "careers@hyd.example")]
    leads = build_rows(repo, "leads", ExportFilter())
    assert {r["name"]: r["linked_startup"] for r in leads} == {"HydCo": True, "NoSite Labs": False}


def test_summarize_stats_labels(repo: Repository, filled: dict[str, int]) -> None:
    summary = summarize_stats(repo, ScoringConfig.load(CONFIG_DIR))
    assert summary["startups_by_score"] == {"high": 1, "medium": 1, "low": 0, "unscored": 1}
    assert summary["leads_without_domain"] == 1
    assert "startup_scores" not in summary


def test_default_export_path() -> None:
    assert str(default_export_path("contacts", "json", today=date(2026, 11, 26))) == (
        "exports/contacts_20261126.json"
    )


# ---------------------------------------------------------------- closing in persist_results
def _result(items: list, scopes: list[str]) -> CollectorResult:
    result: CollectorResult = CollectorResult("greenhouse", started_at=datetime.now(UTC))
    result.items.extend(items)
    result.closable_scopes.extend(scopes)
    return result


def _item(n: int) -> CollectedOpportunity:
    return CollectedOpportunity(
        startup=Startup(name="HydCo", canonical_domain="hyd.example"),
        opportunity=Opportunity(
            source="greenhouse", title=f"Intern {n}", url=f"https://boards.example/greenhouse/{n}"
        ),
    )


def test_vanished_postings_are_closed_only_for_successful_boards(repo: Repository) -> None:
    persist_results([_result([_item(1), _item(2)], ["hyd.example"])], repo)
    (report,) = persist_results([_result([_item(1)], ["hyd.example"])], repo)
    assert report.closed == 1
    statuses = {o.title: o.status for o in repo.list_opportunities()}
    assert statuses == {"Intern 1": OpportunityStatus.OPEN, "Intern 2": OpportunityStatus.CLOSED}

    # A board whose fetch failed has no scope -> nothing is closed.
    (report,) = persist_results([_result([], [])], repo)
    assert report.closed == 0
    # A partially persisted run never closes anything.
    (report,) = persist_results([_result(["not an item"], ["hyd.example"])], repo)
    assert (report.failed, report.closed) == (1, 0)
    assert repo.list_opportunities()[0].status is OpportunityStatus.OPEN
