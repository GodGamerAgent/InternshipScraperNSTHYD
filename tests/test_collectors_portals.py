"""Batch C (ADR 011): Internshala, Cutshort and Instahyre public-listing collectors."""

import json
import logging

import httpx
import pytest

from src.collectors.config import SourcesConfig, StudentPortalsConfig
from src.collectors.cutshort import CutshortCollector, page_jobs
from src.collectors.instahyre import InstahyreCollector, initial_search
from src.collectors.internshala import InternshalaCollector, card_fields, cards
from src.collectors.portal_base import india_portal_location
from src.collectors.registry import ALL_COLLECTOR_NAMES, build_collectors
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import CITY_REMOTE_INDIA, WorkMode
from src.pipeline import persist_results
from src.storage import Repository
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

IS = FIXTURES / "internshala"
CS = FIXTURES / "cutshort"
IH = FIXTURES / "instahyre"
OPEN_ROBOTS = "User-agent: *\nDisallow: /internship/details/\nDisallow: /view/j/\n"


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


def matcher(sources: SourcesConfig) -> InternshipMatcher:
    return InternshipMatcher.from_config(sources.internship)


def internshala(http, sources, geo, cities=None, config=None):
    return InternshalaCollector(
        http,
        config=config or sources.student_portals.internshala,
        geo=geo,
        matcher=matcher(sources),
        cities=cities,
    )


def cutshort(http, sources, geo, cities=None):
    return CutshortCollector(
        http,
        config=sources.student_portals.cutshort,
        geo=geo,
        matcher=matcher(sources),
        non_company_hosts=sources.non_company_hosts,
        cities=cities,
    )


def instahyre(http, sources, geo):
    return InstahyreCollector(
        http, config=sources.student_portals.instahyre, geo=geo, matcher=matcher(sources)
    )


# ---------------------------------------------------------------- shared location rule
def test_india_portal_location(geo: GeoResolver) -> None:
    wfh = india_portal_location(geo, "Work From Home")
    assert wfh is not None
    assert (wfh.city_canonical, wfh.work_mode) == (CITY_REMOTE_INDIA, WorkMode.REMOTE_INDIA)
    hybrid = india_portal_location(geo, "Bengaluru (Bangalore)", workplace="hybrid")
    assert hybrid is not None
    assert (hybrid.city_canonical, hybrid.work_mode) == ("bengaluru", WorkMode.HYBRID)
    assert india_portal_location(geo, "Dubai, United Arab Emirates") is None
    assert india_portal_location(geo, "Singapore", workplace="remote") is None


# ---------------------------------------------------------------- Internshala
def test_internshala_card_fields() -> None:
    first = card_fields(cards((IS / "listing_bangalore_p1.html").read_text())[0])
    assert first["id"] == "9000001"
    assert first["title"] == "Business Development"
    assert first["company"] == "ExampleSales"
    assert first["stipend"] == "₹ 15,000 - 25,000 /month"
    assert first["duration"] == "3 Months"
    assert first["href"].startswith("/internship/detail/")


def test_internshala_parse(
    sources: SourcesConfig, geo: GeoResolver, caplog: pytest.LogCaptureFixture
) -> None:
    raw = [
        ["bengaluru", (IS / "listing_bangalore_p1.html").read_text()],
        ["bengaluru", (IS / "listing_bangalore_p2.html").read_text()],
        ["remote", (IS / "listing_wfh.html").read_text()],
    ]
    with caplog.at_level(logging.WARNING):
        items = list(
            internshala(None, sources, geo).parse(raw, sources.student_portals.internshala)
        )
    jobs = {i.opportunity.title: i.opportunity for i in items}
    # the job card, the card without a link and the repeated card are not separate internships
    assert set(jobs) == {
        "Business Development",
        "Content Writing",
        "Data Analytics",
        "Graphic Design",
    }
    assert "collector.posting_skipped" in caplog.text
    bd = jobs["Business Development"]
    assert (bd.city_canonical, bd.stipend_inr_min, bd.stipend_inr_max, bd.duration_months) == (
        "bengaluru", 15000, 25000, 3,
    )  # fmt: skip
    assert bd.url.startswith("https://internshala.com/internship/detail/business-development")
    assert jobs["Content Writing"].is_paid is False
    assert jobs["Data Analytics"].city_canonical == "hyderabad"
    assert jobs["Graphic Design"].work_mode is WorkMode.REMOTE_INDIA
    assert all(i.startup is None for i in items)


def internshala_handler(seen: list[str], robots: str = OPEN_ROBOTS):
    pages = {
        "/internships/internship-in-bangalore/": "listing_bangalore_p1.html",
        "/internships/internship-in-bangalore/page-2/": "listing_bangalore_p2.html",
        "/internships/work-from-home-internships/": "listing_wfh.html",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        name = pages.get(request.url.path, "listing_empty.html")
        return httpx.Response(200, text=(IS / name).read_text())

    return handler


async def test_internshala_run_paginates_and_narrows_cities(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    async with make_client(internshala_handler(seen), fake_time) as http:
        result = await internshala(http, sources, geo, cities={"bengaluru"}).run()
    assert seen == [
        "/robots.txt",
        "/internships/internship-in-bangalore/",
        "/internships/internship-in-bangalore/page-2/",  # max_pages = 2
        "/internships/work-from-home-internships/",  # remote always kept
        "/internships/work-from-home-internships/page-2/",
    ]
    assert result.issues == []
    assert len(result.items) == 4
    assert result.closable_scopes == ["*"]


async def test_internshala_robots_disallow(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    robots = "User-agent: *\nDisallow: /internships/\n"
    async with make_client(internshala_handler(seen, robots), fake_time) as http:
        result = await internshala(http, sources, geo).run()
    assert seen == ["/robots.txt"]
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "RobotsDisallowedError")]
    assert result.closable_scopes == []


@pytest.mark.parametrize(
    "path", ["/internships/x/?page=2", "/internship/details/abc", "/internships/a#b"]
)
def test_portal_config_rejects_unsafe_paths(path: str) -> None:
    data = SourcesConfig.load(CONFIG_DIR).model_dump(mode="json")
    data["student_portals"]["internshala"]["targets"] = {"bengaluru": path}
    with pytest.raises(ValueError, match="target path"):
        SourcesConfig.model_validate(data)


# ---------------------------------------------------------------- Cutshort
def test_cutshort_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    html = (CS / "remote_jobs.html").read_text()
    assert len(page_jobs(html)) == 4
    items = list(
        cutshort(None, sources, geo).parse([["remote", html]], sources.student_portals.cutshort)
    )
    jobs = {i.opportunity.title: i for i in items}
    # full-time role and the Dubai internship dropped
    assert set(jobs) == {"Business Development Intern", "Frontend Intern"}
    bd = jobs["Business Development Intern"]
    assert bd.opportunity.work_mode is WorkMode.REMOTE_INDIA
    assert (bd.opportunity.stipend_inr_min, bd.opportunity.stipend_inr_max) == (3000, 5000)
    assert bd.startup is not None
    assert (bd.startup.canonical_domain, bd.startup.discovered_via) == (
        "examplebd.example",
        "cutshort",
    )
    frontend = jobs["Frontend Intern"]
    assert frontend.opportunity.city_canonical == "bengaluru"
    assert frontend.startup is None  # a LinkedIn page is not a company website
    dumped = json.dumps([i.model_dump(mode="json") for i in items])
    assert "Fictional Recruiter" not in dumped  # recruiter personal data never stored


def test_cutshort_rejects_missing_next_data() -> None:
    with pytest.raises(ValueError, match="__NEXT_DATA__"):
        page_jobs("<html><body>redesigned</body></html>")


async def test_cutshort_run_one_page_per_target(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=OPEN_ROBOTS)
        return httpx.Response(200, text=(CS / "remote_jobs.html").read_text())

    async with make_client(handler, fake_time) as http:
        result = await cutshort(http, sources, geo, cities={"hyderabad"}).run()
    assert seen == ["/robots.txt", "/jobs/startup-jobs-in-hyderabad", "/jobs/remote-jobs"]
    assert len(result.items) == 2  # the same jobs on both pages are deduplicated
    assert result.closable_scopes == ["*"]


# ---------------------------------------------------------------- Instahyre
def test_instahyre_initial_search_and_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    landing = initial_search((IH / "internship_jobs.html").read_text())
    assert landing["params"]["job_type"] == "2"
    second = json.loads((IH / "job_search_offset2.json").read_text())
    items = list(
        instahyre(None, sources, geo).parse(
            [True, [landing["response"], second]], sources.student_portals.instahyre
        )
    )
    jobs = {i.opportunity.title: i.opportunity for i in items}
    assert set(jobs) == {
        "Data Analyst Intern (Internship)",
        "Marketing Intern (Internship)",
        "Product Intern (Internship)",
    }  # Dubai dropped
    assert jobs["Data Analyst Intern (Internship)"].city_canonical == "bengaluru"
    assert jobs["Marketing Intern (Internship)"].work_mode is WorkMode.REMOTE_INDIA
    assert jobs["Product Intern (Internship)"].city_canonical == "mumbai"
    with pytest.raises(ValueError, match="initial-job-search"):
        initial_search("<html></html>")


async def test_instahyre_follows_only_its_own_next_links(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.path}?{request.url.query.decode()}".rstrip("?"))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n")
        if request.url.path == "/internship-jobs/":
            return httpx.Response(200, text=(IH / "internship_jobs.html").read_text())
        return httpx.Response(200, text=(IH / "job_search_offset2.json").read_text())

    async with make_client(handler, fake_time) as http:
        result = await instahyre(http, sources, geo).run()
    assert seen == [
        "/robots.txt",
        "/internship-jobs/",
        "/api/v1/job_search?company_size=0&isLandingPage=True&job_type=2&limit=2&offset=2",
    ]  # next = null on the second response -> stop
    assert len(result.items) == 3
    assert result.closable_scopes == ["*"]


# ---------------------------------------------------------------- wiring + persistence
def test_registry_and_config(sources: SourcesConfig, geo: GeoResolver) -> None:
    for name in ("internshala", "cutshort", "instahyre"):
        assert name in ALL_COLLECTOR_NAMES
    assert "unstop" not in ALL_COLLECTOR_NAMES  # CloudFront 403 (ADR 011 non-target)
    assert "cuvette" not in ALL_COLLECTOR_NAMES  # domain does not resolve
    portals = sources.student_portals
    for config in (portals.internshala, portals.cutshort):
        assert config is not None
        for key in config.targets:
            assert key == "remote" or geo.city(key)
    built = build_collectors(
        None, ["internshala", "cutshort", "instahyre"], sources=sources, geo=geo, cities={"chennai"}
    )
    assert [c.describe_target(c.targets()[0]) for c in built] == [
        "chennai,remote", "chennai,remote", "internships",
    ]  # fmt: skip
    empty = sources.model_copy(update={"student_portals": StudentPortalsConfig()})
    assert all(
        c.targets() == []
        for c in build_collectors(
            None, ["internshala", "cutshort", "instahyre"], sources=empty, geo=geo
        )
    )


async def test_portal_items_persist(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime, repo: Repository
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=OPEN_ROBOTS)
        return httpx.Response(200, text=(CS / "remote_jobs.html").read_text())

    async with make_client(handler, fake_time) as http:
        result = await cutshort(http, sources, geo, cities=set()).run()
    (report,) = persist_results([result], repo)
    assert (report.opportunities, report.startups, report.failed) == (2, 1, 0)
    assert repo.get_startup_by_domain("examplebd.example") is not None
