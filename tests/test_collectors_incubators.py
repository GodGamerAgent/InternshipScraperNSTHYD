"""IITM Incubation Cell (JSON API) and NSRCEL (popup HTML) collectors, ADR 008."""

import sqlite3
from pathlib import Path

import httpx
import pytest

from src.collectors.config import IncubatorConfig, SourcesConfig
from src.collectors.iitm import IitmCollector, portfolio_rows
from src.collectors.nsrcel import NsrcelCollector, popup_fields, popups, venture_stage
from src.collectors.thub import ThubCollector
from src.enrichment.geo import GeoResolver
from src.models import Country, FundingStage, StartupLead
from src.storage import Repository
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

INC = FIXTURES / "incubators"
IITM = IncubatorConfig(
    name="iitm_incubation",
    kind="iitm_api",
    url="https://incubation.iitm.ac.in/api/portfolios/active",
    default_city="chennai",
    page_size=3,
    max_pages=5,
)
NSRCEL = IncubatorConfig(
    name="nsrcel",
    kind="nsrcel_popup",
    url="https://nsrcel.org/portfolio/",
    default_city="bengaluru",
)


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


# ---------------------------------------------------------------- IITM
def iitm_handler(seen: list[str], robots: str = "<!doctype html><html>SPA shell</html>"):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.path}?{request.url.query.decode()}".rstrip("?"))
        if request.url.path == "/robots.txt":  # the live site answers with its SPA shell
            return httpx.Response(200, text=robots)
        page = request.url.params["page"]
        return httpx.Response(200, text=(INC / f"iitm_page{page}.json").read_text())

    return handler


async def test_iitm_paginates_and_builds_leads(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    collector = IitmCollector(
        None, incubators=[IITM, NSRCEL], geo=geo, non_company_hosts=sources.non_company_hosts
    )
    assert collector.targets() == [IITM]  # other kinds belong to other collectors
    async with make_client(iitm_handler(seen), fake_time) as http:
        collector.http = http
        result = await collector.run()

    assert seen == [
        "/robots.txt",  # robots respected (HTML fallback = no rules = allowed)
        "/api/portfolios/active?page=1&limit=3",
        "/api/portfolios/active?page=2&limit=3",
    ]
    assert result.issues == []
    leads = {lead.name: lead for lead in result.items}
    # inactive and nameless rows dropped
    assert set(leads) == {
        "Examplesense Technologies Private Limited",
        "Sampleflow Labs",
        "Elsewhere Robotics",
        "Linkedonly Analytics",
    }
    sense = leads["Examplesense Technologies Private Limited"]
    assert isinstance(sense, StartupLead)
    assert (sense.domain, sense.founders, sense.sector) == (
        "examplesense.example", ["Asha Fictional"], "Deep Tech",
    )  # fmt: skip
    assert (sense.hq_country, sense.hq_city_canonical) == (Country.IN, "chennai")  # "India" only
    assert sense.source == "iitm"
    assert sense.source_url == "https://incubation.iitm.ac.in/"
    assert leads["Sampleflow Labs"].hq_city_canonical == "chennai"  # no location at all
    assert leads["Sampleflow Labs"].founders == ["Ravi Placeholder", "Meera Sample"]
    assert leads["Elsewhere Robotics"].hq_city_canonical == "bengaluru"  # specific city wins
    assert leads["Linkedonly Analytics"].domain is None  # social profile is not a website


async def test_iitm_robots_disallow_is_respected(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    async with make_client(
        iitm_handler(seen, "User-agent: *\nDisallow: /api/\n"), fake_time
    ) as http:
        result = await IitmCollector(http, incubators=[IITM], geo=geo).run()
    assert seen == ["/robots.txt"]
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "RobotsDisallowedError")]


def test_iitm_rejects_unexpected_shape() -> None:
    with pytest.raises(ValueError, match="IITM portfolio API"):
        portfolio_rows({"items": []})


# ---------------------------------------------------------------- NSRCEL
def test_popups_are_outermost_and_fields_parsed() -> None:
    found = popups((INC / "nsrcel_portfolio.html").read_text())
    assert len(found) == 4  # the nested "*-startup-popup-details" block is not a second startup
    fields = popup_fields(found[0])
    assert fields["name"] == "ExampleWealth"
    assert fields["stage of venture"] == "Pre-Series A"
    assert fields["hrefs"] == ["https://www.examplewealth.example"]
    assert venture_stage("Bootstrapped") is FundingStage.BOOTSTRAPPED
    assert venture_stage("Seed") is FundingStage.SEED
    assert venture_stage("") is None


def nsrcel_handler(seen: list[str], html: str):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /wp-admin/\n")
        return httpx.Response(200, text=html)

    return handler


async def test_nsrcel_builds_leads_with_default_city(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    html = (INC / "nsrcel_portfolio.html").read_text()
    async with make_client(nsrcel_handler(seen, html), fake_time) as http:
        result = await NsrcelCollector(
            http, incubators=[IITM, NSRCEL], geo=geo, non_company_hosts=sources.non_company_hosts
        ).run()
    assert seen == ["/robots.txt", "/portfolio/"]
    leads = {lead.name: lead for lead in result.items}
    assert set(leads) == {"ExampleWealth", "SampleLend", "NoSite Pay"}  # blank name skipped
    wealth = leads["ExampleWealth"]
    assert wealth.domain == "examplewealth.example"
    assert wealth.founders == ["Asha Fictional", "Ravi Placeholder", "Meera Sample"]
    assert (wealth.sector, wealth.funding_stage) == ("Wealth Tech", FundingStage.SEED)
    assert (wealth.hq_country, wealth.hq_city_canonical) == (Country.IN, "bengaluru")
    assert leads["SampleLend"].domain == "samplelend.example"  # plain-text website
    assert leads["SampleLend"].funding_stage is FundingStage.BOOTSTRAPPED
    assert leads["NoSite Pay"].domain is None  # link back to nsrcel.org is not the startup's


class FakeRenderer:
    def __init__(self, html: str) -> None:
        self.html = html
        self.calls: list[str] = []

    async def scrape_html(self, url: str) -> str:
        self.calls.append(url)
        return self.html


async def test_nsrcel_js_only_page_needs_renderer(geo: GeoResolver, fake_time: FakeTime) -> None:
    shell = "<html><body><div id='root'></div></body></html>"
    async with make_client(nsrcel_handler([], shell), fake_time) as http:
        result = await NsrcelCollector(http, incubators=[NSRCEL], geo=geo).run()
    assert [(i.stage, i.error_type) for i in result.issues] == [
        ("fetch", "JavaScriptRequiredError")
    ]
    renderer = FakeRenderer((INC / "nsrcel_portfolio.html").read_text())
    async with make_client(nsrcel_handler([], shell), fake_time) as http:
        rendered = await NsrcelCollector(
            http, incubators=[NSRCEL], geo=geo, renderer=renderer
        ).run()
    assert renderer.calls == [NSRCEL.url]
    assert len(rendered.items) == 3


# ---------------------------------------------------------------- wiring
def test_thub_only_takes_its_own_kinds(sources: SourcesConfig, geo: GeoResolver) -> None:
    thub = ThubCollector(None, incubators=sources.incubators, geo=geo)
    assert [t.name for t in thub.targets()] == ["thub_lab32", "thub"]
    names = {i.name: i for i in sources.incubators}
    assert names["iitm_incubation"].default_city == "chennai"
    assert names["nsrcel"].default_city == "bengaluru"


def test_old_database_gets_new_lead_columns(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    Repository.open(db).close()
    with sqlite3.connect(db) as conn:  # simulate a pre-v0.2 file
        for column in ("batch", "is_hiring", "team_size", "github_org"):
            conn.execute(f"ALTER TABLE leads DROP COLUMN {column}")
    with Repository.open(db) as repo:
        repo.upsert_lead(
            StartupLead(name="ExampleCo", source="yc_india", batch="w2024", is_hiring=True)
        )
        lead = repo.get_lead_by_name("ExampleCo")
    assert lead is not None
    assert (lead.batch, lead.is_hiring) == ("w2024", True)
