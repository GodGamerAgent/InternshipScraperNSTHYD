"""YC India directory collector (ADR 008): data-page JSON, pagination, hiring flag, robots."""

import httpx
import pytest

from src.collectors.config import SourcesConfig, YcDirectoryConfig
from src.collectors.yc_india import YcIndiaCollector, page_props
from src.enrichment.geo import GeoResolver
from src.models import Country, StartupLead
from src.pipeline import persist_results
from src.storage import Repository
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

YC = FIXTURES / "yc_india"
CONFIG = YcDirectoryConfig(
    listing_url="https://www.ycombinator.com/companies/location/india",
    hiring_url="https://www.ycombinator.com/companies/location/india/hiring",
    max_pages=5,
    statuses=["Active", "Public"],
)


def yc_handler(seen: list[str], robots: str = "User-agent: *\nDisallow: /search\n"):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.path}?{request.url.query.decode()}".rstrip("?"))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        page = request.url.params.get("page", "1")
        if request.url.path == "/companies/location/india/hiring":
            return httpx.Response(200, text=(YC / "hiring_page1.html").read_text())
        if request.url.path == "/companies/location/india":
            return httpx.Response(200, text=(YC / f"listing_page{page}.html").read_text())
        return httpx.Response(404)

    return handler


def make(http, geo: GeoResolver, config: YcDirectoryConfig | None = CONFIG) -> YcIndiaCollector:
    sources = SourcesConfig.load(CONFIG_DIR)
    return YcIndiaCollector(
        http, config=config, geo=geo, non_company_hosts=sources.non_company_hosts
    )


def test_page_props_reads_data_page() -> None:
    props = page_props((YC / "listing_page1.html").read_text())
    assert props["hasMore"] is True
    assert len(props["companies"]) == 3
    with pytest.raises(ValueError, match="no data-page"):
        page_props("<html><body>redesigned</body></html>")
    with pytest.raises(ValueError, match="malformed"):
        page_props('<div data-page="{oops"></div>')
    with pytest.raises(ValueError, match=r"no props\.companies"):
        page_props("<div data-page='{\"props\": {}}'></div>")


async def test_run_paginates_and_marks_hiring(geo: GeoResolver, fake_time: FakeTime) -> None:
    seen: list[str] = []
    async with make_client(yc_handler(seen), fake_time) as http:
        result = await make(http, geo).run()

    assert seen == [
        "/robots.txt",
        "/companies/location/india/hiring?page=1",  # hasMore false -> one page
        "/companies/location/india?page=1",
        "/companies/location/india?page=2",
    ]
    assert result.issues == []
    assert result.closable_scopes == []
    leads = {lead.name: lead for lead in result.items}
    # Inactive company and the non-IN company are dropped; hiring-only company is kept.
    assert set(leads) == {"ExamplePay", "SampleHealth", "DemoMart", "HireOnly AI"}

    pay = leads["ExamplePay"]
    assert isinstance(pay, StartupLead)
    assert (pay.domain, pay.batch, pay.is_hiring, pay.team_size, pay.github_org) == (
        "examplepay.example", "w2021", True, 50, "examplepay",
    )  # fmt: skip
    assert (pay.hq_country, pay.hq_city_canonical) == (Country.IN, "bengaluru")
    assert pay.source == "yc_india"
    assert pay.source_url == "https://www.ycombinator.com/companies/examplepay"
    assert pay.sector == "fintech, b2b"

    assert leads["SampleHealth"].hq_city_canonical == "delhi_ncr"
    assert leads["SampleHealth"].is_hiring is False
    assert leads["SampleHealth"].github_org is None
    demo = leads["DemoMart"]
    assert (demo.domain, demo.team_size, demo.is_hiring) == (None, None, False)
    assert leads["HireOnly AI"].is_hiring is True
    assert leads["HireOnly AI"].hq_city_canonical == "hyderabad"


async def test_without_hiring_url_hiring_is_unknown(geo: GeoResolver, fake_time: FakeTime) -> None:
    seen: list[str] = []
    config = CONFIG.model_copy(update={"hiring_url": None})
    async with make_client(yc_handler(seen), fake_time) as http:
        result = await make(http, geo, config).run()
    assert not any("hiring" in path for path in seen)
    assert {lead.is_hiring for lead in result.items} == {None}


async def test_robots_disallow_stops_before_any_page(geo: GeoResolver, fake_time: FakeTime) -> None:
    seen: list[str] = []
    async with make_client(
        yc_handler(seen, "User-agent: *\nDisallow: /companies\n"), fake_time
    ) as http:
        result = await make(http, geo).run()
    assert seen == ["/robots.txt"]
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "RobotsDisallowedError")]


async def test_no_config_means_no_targets(geo: GeoResolver, fake_time: FakeTime) -> None:
    async with make_client(yc_handler([]), fake_time) as http:
        result = await make(http, geo, None).run()
    assert result.targets_total == 0
    assert result.items == []


async def test_leads_promote_to_startups_with_yc_facts(
    geo: GeoResolver, fake_time: FakeTime, repo: Repository
) -> None:
    async with make_client(yc_handler([]), fake_time) as http:
        result = await make(http, geo).run()
    (report,) = persist_results([result], repo)
    assert (report.leads, report.startups, report.failed) == (4, 3, 0)  # DemoMart has no domain
    startup = repo.get_startup_by_domain("examplepay.example")
    assert startup is not None
    assert (startup.team_size, startup.github_org, startup.discovered_via) == (
        50,
        "examplepay",
        "yc_india",
    )
    lead = repo.get_lead_by_name("ExamplePay")
    assert lead is not None
    assert (lead.batch, lead.is_hiring) == ("w2021", True)
