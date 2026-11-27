"""Offline tests for ``src/pipeline.py`` — collectors -> repository."""

from datetime import UTC, datetime

import pytest

from src.collectors.config import SourcesConfig
from src.collectors.greenhouse import GreenhouseCollector
from src.collectors.lever import LeverCollector
from src.core.base import CollectorResult
from src.core.http import HttpClient
from src.enrichment.geo import GeoResolver
from src.pipeline import collect_and_store, persist_results
from src.storage import Repository
from tests.conftest import CONFIG_DIR, FakeTime, make_client
from tests.test_collectors_ats import SEEDS, fixture_handler, load, make_collector


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


async def test_collect_and_store_end_to_end_is_idempotent(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime, repo: Repository
) -> None:
    async with make_client(fixture_handler([]), fake_time) as http:
        collectors = [
            make_collector(GreenhouseCollector, http, sources, geo),
            make_collector(LeverCollector, http, sources, geo),
        ]
        results, reports = await collect_and_store(collectors, repo)
        assert [r.collector for r in results] == ["greenhouse", "lever"]
        assert [(r.startups, r.opportunities, r.failed) for r in reports] == [(1, 3, 0), (1, 2, 0)]
        first_counts = repo.counts()

        await collect_and_store(collectors, repo)  # second daily run
    assert (
        repo.counts()
        == first_counts
        == {"startups": 2, "opportunities": 5, "contacts": 0, "leads": 0}
    )

    startup = repo.get_startup_by_domain(SEEDS["greenhouse"].domain)
    assert startup is not None
    assert startup.last_checked_at is not None
    opps = repo.list_opportunities(startup_id=startup.id)
    assert {o.title for o in opps} == {
        "Software Engineering Intern",
        "Product Design",
        "Growth Intern",
    }
    assert all(o.startup_id == startup.id for o in opps)


def test_persist_counts_bad_items_without_aborting(repo: Repository) -> None:
    result: CollectorResult[object] = CollectorResult("weird", started_at=datetime.now(UTC))
    result.items.append({"not": "a CollectedOpportunity"})
    (report,) = persist_results([result], repo)
    assert (report.opportunities, report.failed) == (0, 1)
    assert "TypeError" in report.errors[0]


def test_persist_stamps_checked_at(
    sources: SourcesConfig, geo: GeoResolver, repo: Repository
) -> None:
    http = HttpClient(user_agent="india-internship-scraper/0.1")  # parse() needs no network
    collector = make_collector(GreenhouseCollector, http, sources, geo)
    result: CollectorResult[object] = CollectorResult("greenhouse", started_at=datetime.now(UTC))
    result.items.extend(collector.parse(load("greenhouse"), SEEDS["greenhouse"]))
    stamp = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)
    persist_results([result], repo, checked_at=stamp)
    startup = repo.get_startup_by_domain("examplelabs.example")
    assert startup is not None
    assert startup.last_checked_at == stamp
