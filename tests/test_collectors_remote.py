"""Batch B (ADR 010): Himalayas + Remotive remote internships open to India."""

import json
import logging

import httpx
import pytest

from src.collectors.config import RemoteFeedsConfig, SourcesConfig
from src.collectors.himalayas import HimalayasCollector
from src.collectors.registry import ALL_COLLECTOR_NAMES, build_collectors
from src.collectors.remote_base import IndiaOpenness
from src.collectors.remotive import RemotiveCollector
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import CITY_REMOTE_INDIA, Country, WorkMode
from src.pipeline import persist_results
from src.storage import Repository
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

HIM = FIXTURES / "himalayas"
REMOTIVE_FEED = (FIXTURES / "remotive" / "feed.xml").read_bytes()


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


def himalayas(
    http, sources: SourcesConfig, geo: GeoResolver, config: RemoteFeedsConfig | None = None
):
    matcher = InternshipMatcher.from_config(sources.internship)
    return HimalayasCollector(http, config=config or sources.remote_feeds, geo=geo, matcher=matcher)


def remotive(
    http, sources: SourcesConfig, geo: GeoResolver, config: RemoteFeedsConfig | None = None
):
    matcher = InternshipMatcher.from_config(sources.internship)
    return RemotiveCollector(http, config=config or sources.remote_feeds, geo=geo, matcher=matcher)


def him_raw() -> list[dict]:
    return [
        json.loads((HIM / f).read_text()) for f in ("search_india.json", "search_worldwide.json")
    ]


# ---------------------------------------------------------------- openness rules
def test_india_openness_rules(sources: SourcesConfig, geo: GeoResolver) -> None:
    rules = IndiaOpenness(sources.remote_feeds, geo)
    india = rules.structured(["India", "United States"], [])
    assert india is not None
    assert india.work_mode is WorkMode.REMOTE_INDIA
    assert india.country is Country.IN
    ist = rules.structured([], [5.5])
    assert ist is not None
    assert ist.work_mode is WorkMode.REMOTE_INDIA
    world = rules.structured([], [])
    assert world is not None
    assert world.work_mode is WorkMode.REMOTE_GLOBAL
    assert rules.structured(["United States"], []) is None
    assert rules.structured([], [8, 9]) is None  # timezone-locked away from IST
    assert rules.free_text("India").work_mode is WorkMode.REMOTE_INDIA
    assert rules.free_text("Worldwide").work_mode is WorkMode.REMOTE_GLOBAL
    assert rules.free_text("USA, Canada") is None
    assert rules.free_text("LATAM, Europe") is None

    strict = IndiaOpenness(sources.remote_feeds.model_copy(update={"keep_worldwide": False}), geo)
    assert strict.structured([], []) is None
    assert strict.free_text("Worldwide") is None
    assert strict.free_text("India") is not None


# ---------------------------------------------------------------- Himalayas
def test_himalayas_parse(
    sources: SourcesConfig, geo: GeoResolver, caplog: pytest.LogCaptureFixture
) -> None:
    collector = himalayas(None, sources, geo)
    with caplog.at_level(logging.WARNING):
        items = list(collector.parse(him_raw(), sources.remote_feeds.himalayas))
    jobs = {i.opportunity.title: i for i in items}
    # US-only, timezone-locked, non-intern and malformed jobs dropped; duplicate deduped
    assert set(jobs) == {"Frontend Intern", "Growth Intern", "Research Intern"}
    assert "collector.posting_skipped" in caplog.text
    frontend = jobs["Frontend Intern"]
    assert frontend.startup is None  # no verifiable company domain in the feed
    opp = frontend.opportunity
    assert opp.url == "https://himalayas.app/companies/exampleremote/jobs/frontend-intern"
    assert (opp.city_canonical, opp.work_mode) == (CITY_REMOTE_INDIA, WorkMode.REMOTE_INDIA)
    assert (opp.stipend_inr_min, opp.stipend_inr_max, opp.duration_months) == (15000, 20000, 3)
    assert opp.posted_at is not None
    assert opp.posted_at.year == 2026  # epoch seconds, not milliseconds
    assert opp.description is not None
    assert opp.description.startswith("ExampleRemote")
    assert jobs["Growth Intern"].opportunity.work_mode is WorkMode.REMOTE_INDIA  # IST-only
    assert jobs["Research Intern"].opportunity.work_mode is WorkMode.REMOTE_GLOBAL


def him_handler(seen: list[str], robots: str = "User-agent: *\nAllow: /\nDisallow: /jobs*&page=\n"):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.path}?{request.url.query.decode()}".rstrip("?"))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        name = "search_india.json" if request.url.params.get("country") else "search_worldwide.json"
        return httpx.Response(200, text=(HIM / name).read_text())

    return handler


async def test_himalayas_run_page_one_only_with_robots(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    async with make_client(him_handler(seen), fake_time) as http:
        result = await himalayas(http, sources, geo).run()
    assert seen[0] == "/robots.txt"
    assert seen[1:] == [
        "/jobs/api/search?country=IN&employment_type=Intern&sort=recent",
        "/jobs/api/search?worldwide=true&employment_type=Intern&sort=recent",
    ]
    assert not any("page=" in s for s in seen)
    assert len(result.items) == 3
    assert result.closable_scopes == ["*"]


async def test_himalayas_robots_disallow(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    async with make_client(
        him_handler(seen, "User-agent: *\nDisallow: /jobs/api\n"), fake_time
    ) as http:
        result = await himalayas(http, sources, geo).run()
    assert seen == ["/robots.txt"]
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "RobotsDisallowedError")]
    assert result.closable_scopes == []  # never close after a failed run


def test_himalayas_config_rejects_paging() -> None:
    data = SourcesConfig.load(CONFIG_DIR).model_dump(mode="json")
    data["remote_feeds"]["himalayas"]["queries"] = [{"q": "intern", "page": "2"}]
    with pytest.raises(ValueError, match="must not page"):
        SourcesConfig.model_validate(data)


def test_himalayas_rejects_unexpected_shape(sources: SourcesConfig, geo: GeoResolver) -> None:
    with pytest.raises(ValueError, match="Himalayas search API"):
        list(himalayas(None, sources, geo).parse([{"oops": []}], sources.remote_feeds.himalayas))


# ---------------------------------------------------------------- Remotive
def test_remotive_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    items = list(remotive(None, sources, geo).parse(REMOTIVE_FEED, sources.remote_feeds.remotive))
    jobs = {i.opportunity.title: i.opportunity for i in items}
    # USA-only, LATAM/Europe and the full-time role are dropped
    assert set(jobs) == {"Data Intern", "Marketing Intern"}
    assert jobs["Data Intern"].work_mode is WorkMode.REMOTE_GLOBAL
    marketing = jobs["Marketing Intern"]
    assert marketing.work_mode is WorkMode.REMOTE_INDIA
    assert (marketing.stipend_inr_min, marketing.duration_months) == (12000, 6)
    assert marketing.url == "https://remotive.com/remote-jobs/software-dev/marketing-intern-9002"
    assert marketing.posted_at is not None
    assert marketing.posted_at.isoformat() == "2026-11-25T11:26:35+00:00"


def rem_handler(seen: list[str], robots: str):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(
            200, content=REMOTIVE_FEED, headers={"content-type": "application/rss+xml"}
        )

    return handler


async def test_remotive_run_uses_rss_not_api(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    robots = "User-agent: *\nDisallow: /api/*\nDisallow: /v2/feed\n"  # mirrors remotive.com
    async with make_client(rem_handler(seen, robots), fake_time) as http:
        result = await remotive(http, sources, geo).run()
    assert seen == ["/robots.txt", "/remote-jobs/feed"]
    assert len(result.items) == 2
    assert result.closable_scopes == ["*"]


async def test_remotive_robots_disallow(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    async with make_client(
        rem_handler(seen, "User-agent: *\nDisallow: /remote-jobs/\n"), fake_time
    ) as http:
        result = await remotive(http, sources, geo).run()
    assert seen == ["/robots.txt"]
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "RobotsDisallowedError")]


def test_remotive_config_cannot_point_at_api() -> None:
    data = SourcesConfig.load(CONFIG_DIR).model_dump(mode="json")
    data["remote_feeds"]["remotive"]["feed_url"] = "https://remotive.com/api/remote-jobs"
    with pytest.raises(ValueError, match="feed_url"):
        SourcesConfig.model_validate(data)


# ---------------------------------------------------------------- wiring + persistence
def test_registry_builds_remote_collectors(sources: SourcesConfig, geo: GeoResolver) -> None:
    assert {"himalayas", "remotive"} <= set(ALL_COLLECTOR_NAMES)
    built = build_collectors(None, ["himalayas", "remotive"], sources=sources, geo=geo)
    assert [type(c) for c in built] == [HimalayasCollector, RemotiveCollector]
    empty = sources.model_copy(update={"remote_feeds": RemoteFeedsConfig()})
    assert [
        c.targets()
        for c in build_collectors(None, ["himalayas", "remotive"], sources=empty, geo=geo)
    ] == [[], []]


async def test_remote_items_persist_without_startup(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime, repo: Repository
) -> None:
    async with make_client(him_handler([]), fake_time) as http:
        result = await himalayas(http, sources, geo).run()
    (report,) = persist_results([result], repo)
    assert (report.opportunities, report.startups, report.failed) == (3, 0, 0)
