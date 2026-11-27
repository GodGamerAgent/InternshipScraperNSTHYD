"""Offline tests for Phase 4 discovery collectors: funding news, T-Hub, HN "Who is hiring?"."""

import json
from datetime import date
from typing import Any

import httpx
import pytest

from src.collectors.config import SourcesConfig
from src.collectors.funding_news import FundingNewsCollector
from src.collectors.hn_hiring import HnHiringCollector
from src.collectors.registry import build_collectors
from src.collectors.thub import ThubCollector
from src.core.http import HttpClient
from src.enrichment.funding import detect_stage, parse_funding_headline
from src.enrichment.geo import GeoResolver
from src.models import CITY_REMOTE_INDIA, Country, FundingStage, WorkMode
from src.pipeline import collect_and_store
from src.storage import Repository
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

OPEN_ROBOTS = "User-agent: *\nAllow: /\n"


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


def offline_http() -> HttpClient:
    return HttpClient(user_agent="india-internship-scraper/0.1")  # parse() never uses it


def text(path: str) -> str:
    return (FIXTURES / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------- funding headline parser
@pytest.mark.parametrize(
    ("title", "summary", "company", "amount", "stage", "city"),
    [
        ("GIVA raises Rs 90 Cr from BlackSoil", "", "GIVA", "Rs 90 Cr", None, None),
        ("Green Aero raises $14 Mn led by Crane", "", "Green Aero", "$14 Mn", None, None),
        ("XEBO.ai Bags $10.5 Mn To Expand", "", "XEBO.ai", "$10.5 Mn", None, None),
        ("Exclusive: TinyLedger bags $1.2 Mn in pre-seed round", "", "TinyLedger", "$1.2 Mn",
         FundingStage.PRE_SEED, None),
        ("Bengaluru-based fintech startup Finnable secures ₹405 Cr", "", "Finnable", "₹405 Cr",
         None, "Bengaluru"),
        ("D2C brand Sproutly picks up Rs 4 Cr in pre-Series A", "", "Sproutly", "Rs 4 Cr",
         FundingStage.SEED, None),
        ("Acme raises $3 Mn", "Pune-based Acme raised a Series A. It earlier raised a seed round.",
         "Acme", "$3 Mn", FundingStage.SERIES_A, "Pune"),
    ],
)  # fmt: skip
def test_parse_funding_headline(title, summary, company, amount, stage, city) -> None:
    event = parse_funding_headline(title, summary)
    assert event is not None
    assert (event.company, event.amount, event.stage, event.city) == (company, amount, stage, city)


@pytest.mark.parametrize(
    "title",
    [
        "Funding Galore: Indian Startups Raised $200 Mn This Week",
        "Uber appoints former Zomato CEO as India president",
        "Alpha and Beta raise $5 Mn each",
        "These 5 startups raised Rs 100 Cr",
        "flipkart raises prices",  # no amount
    ],
)
def test_non_single_company_headlines_rejected(title: str) -> None:
    assert parse_funding_headline(title) is None


def test_detect_stage_prefers_first_mention() -> None:
    assert detect_stage("Series B round; the seed was in 2021") is FundingStage.SERIES_B
    assert detect_stage("pre-seed") is FundingStage.PRE_SEED
    assert detect_stage("Series D") is FundingStage.SERIES_C_PLUS
    assert detect_stage("debt round") is None


# ---------------------------------------------------------------- funding news collector
def test_entrackr_feed_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    feed = next(f for f in sources.funding_feeds if f.name == "entrackr")
    collector = FundingNewsCollector(offline_http(), feeds=[feed], geo=geo)
    leads = {lead.name: lead for lead in collector.parse(text("funding/entrackr_rss.xml"), feed)}
    assert set(leads) == {"ExampleGems", "Skyward Aero", "TinyLedger", "ChaiOps"}

    gems = leads["ExampleGems"]
    assert (gems.funding_amount, gems.funding_stage) == ("Rs 90 Cr", FundingStage.SERIES_B)
    assert gems.hq_city_canonical == "bengaluru"
    assert gems.last_funded_at == date(2026, 11, 26)
    assert gems.source == "entrackr"
    assert gems.domain is None  # news never gives the company website
    assert leads["Skyward Aero"].funding_stage is FundingStage.SERIES_A  # not the earlier seed
    assert leads["Skyward Aero"].hq_city_canonical == "hyderabad"
    assert leads["TinyLedger"].funding_stage is FundingStage.PRE_SEED
    assert (leads["ChaiOps"].funding_stage, leads["ChaiOps"].hq_city_canonical) == (
        FundingStage.SEED,
        "chennai",
    )


def test_inc42_feed_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    feed = next(f for f in sources.funding_feeds if f.name == "inc42")
    collector = FundingNewsCollector(offline_http(), feeds=[feed], geo=geo)
    (lead,) = collector.parse(text("funding/inc42_buzz.xml"), feed)
    assert (lead.name, lead.funding_amount) == ("XampleAI", "$10.5 Mn")
    assert lead.hq_city_canonical == "delhi_ncr"


def test_garbage_feed_is_a_parse_error(sources: SourcesConfig, geo: GeoResolver) -> None:
    collector = FundingNewsCollector(offline_http(), feeds=sources.funding_feeds, geo=geo)
    with pytest.raises(ValueError, match="RSS"):
        list(collector.parse("<html><body>not a feed", sources.funding_feeds[0]))


def site_handler(pages: dict[str, str], seen: list[str], robots: str = OPEN_ROBOTS):
    """Serve ``pages`` by path, robots.txt from ``robots``; 404 otherwise."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        body = pages.get(request.url.path)
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

    return handler


async def test_feeds_respect_robots(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    pages = {"/rss": text("funding/entrackr_rss.xml")}
    feeds = [f for f in sources.funding_feeds if f.name == "entrackr"]
    async with make_client(site_handler(pages, seen), fake_time) as http:
        result = await FundingNewsCollector(http, feeds=feeds, geo=geo).run()
    assert len(result.items) == 4
    assert seen[0] == "entrackr.com/robots.txt"

    seen.clear()
    blocked = site_handler(pages, seen, robots="User-agent: *\nDisallow: /\n")
    async with make_client(blocked, fake_time) as http:
        result = await FundingNewsCollector(http, feeds=feeds, geo=geo).run()
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "RobotsDisallowedError")]
    assert seen == ["entrackr.com/robots.txt"]  # the feed itself was never requested


# ---------------------------------------------------------------- T-Hub
def test_lab32_tablepress_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    lab32 = next(i for i in sources.incubators if i.name == "thub_lab32")
    collector = ThubCollector(
        offline_http(),
        incubators=[lab32],
        geo=geo,
        non_company_hosts=sources.non_company_hosts,
    )
    leads = {lead.name: lead for lead in collector.parse(text("thub/lab32_directory.html"), lab32)}
    assert set(leads) == {
        "ExampleLabs Technologies Pvt Ltd",
        "Registry Linked Pvt Ltd",
        "Plain Text Site",
        "NoWebsite Robotics",
        "Island Ventures",
        "Gurgaon Mobility",
    }
    labs = leads["ExampleLabs Technologies Pvt Ltd"]
    assert labs.domain == "examplelabs.example"
    assert labs.founders == ["Asha Rao", "Vikram Iyer"]
    assert (labs.hq_city_canonical, labs.sector) == ("hyderabad", "Health Tech")
    assert labs.source_url == f"{lab32.url}#tablepress-1"

    assert leads["Registry Linked Pvt Ltd"].domain is None  # zaubacorp is not the company site
    assert leads["Plain Text Site"].domain == "agritech.example"
    assert leads["Plain Text Site"].hq_city_canonical == "hyderabad"  # empty HQ -> T-Hub default
    assert leads["NoWebsite Robotics"].hq_city_canonical == "bengaluru"
    assert leads["NoWebsite Robotics"].founders == ["Divya Nair", "Arjun Das"]
    island = leads["Island Ventures"]  # Colombo must NOT default to Hyderabad
    assert island.hq_city_canonical == "unknown"
    assert island.hq_country is None
    assert leads["Gurgaon Mobility"].hq_state == "Haryana"


async def test_webflow_directory_requires_renderer(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    thub = next(i for i in sources.incubators if i.name == "thub")
    pages = {"/startups-directory": text("thub/startups_directory_shell.html")}
    async with make_client(site_handler(pages, []), fake_time) as http:
        result = await ThubCollector(http, incubators=[thub], geo=geo).run()
    assert result.items == []
    assert [(i.stage, i.error_type) for i in result.issues] == [
        ("fetch", "JavaScriptRequiredError")
    ]


class FakeRenderer:
    def __init__(self, html: str) -> None:
        self.html = html
        self.urls: list[str] = []

    async def scrape_html(self, url: str) -> str:
        self.urls.append(url)
        return self.html


async def test_webflow_directory_via_renderer(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    thub = next(i for i in sources.incubators if i.name == "thub")
    renderer = FakeRenderer(text("thub/startups_directory_rendered.html"))
    pages = {"/startups-directory": text("thub/startups_directory_shell.html")}
    async with make_client(site_handler(pages, []), fake_time) as http:
        collector = ThubCollector(
            http,
            incubators=[thub],
            geo=geo,
            renderer=renderer,
            non_company_hosts=sources.non_company_hosts,
        )
        result = await collector.run()
    assert renderer.urls == [thub.url]
    leads = {lead.name: lead for lead in result.items}
    assert leads["Hitec Drones"].domain == "hitecdrones.example"
    assert leads["Hitec Drones"].founders == ["Meera Reddy"]
    assert leads["Secunderabad Foods"].domain is None  # LinkedIn + T-Hub profile links skipped
    assert leads["Secunderabad Foods"].hq_city_canonical == "hyderabad"


# ---------------------------------------------------------------- HN "Who is hiring?"
def hn_raw() -> dict[str, Any]:
    stories = json.loads(text("hn/stories.json"))
    return {"story": stories["hits"][1], "comments": json.loads(text("hn/comments.json"))["hits"]}


def hn_collector(sources: SourcesConfig, geo: GeoResolver, http: HttpClient | None = None):
    return HnHiringCollector(
        http or offline_http(),
        config=sources.hacker_news,
        geo=geo,
        non_company_hosts=sources.non_company_hosts,
    )


def test_hn_parse_filters_and_extracts(sources: SourcesConfig, geo: GeoResolver) -> None:
    items = list(hn_collector(sources, geo).parse(hn_raw(), None))
    by_title = {i.opportunity.title: i for i in items}
    assert set(by_title) == {"Backend Engineering Intern", "New Grad Engineer", "Robotics Intern"}

    stack = by_title["Backend Engineering Intern"]
    assert stack.startup is not None
    assert stack.startup.canonical_domain == "examplestack.example"  # lever link skipped
    opp = stack.opportunity
    assert (opp.city_canonical, opp.work_mode) == ("bengaluru", WorkMode.ONSITE)
    assert (opp.stipend_inr_min, opp.duration_months, opp.has_ppo) == (40000, 6, True)
    assert opp.url == "https://news.ycombinator.com/item?id=1"
    assert opp.source == "hn_hiring"

    worldwide = by_title["New Grad Engineer"].opportunity
    assert worldwide.country is Country.GLOBAL
    assert worldwide.work_mode is WorkMode.REMOTE_GLOBAL

    robotics = by_title["Robotics Intern"]
    assert robotics.startup is None  # only a LinkedIn link
    assert robotics.opportunity.city_canonical == CITY_REMOTE_INDIA
    assert robotics.opportunity.is_paid is False


def test_hn_never_keeps_job_seeker_posts(sources: SourcesConfig, geo: GeoResolver) -> None:
    collector = hn_collector(sources, geo)
    items = collector.parse(hn_raw(), None)
    descriptions = " ".join(i.opportunity.description or "" for i in items)
    assert "seeker@example.invalid" not in descriptions
    assert "Willing to relocate" not in descriptions
    assert collector.is_seeker_post("Location: Pune\nRemote: Yes\nWilling to relocate: No")
    assert collector.is_seeker_post("SEEKING WORK | Fresher | Remote")
    assert not collector.is_seeker_post("ExampleStack | Intern | Bengaluru")


async def test_hn_fetch_picks_hiring_thread(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)  # hn.algolia.com has none -> allowed
        if request.url.path == "/api/v1/search_by_date":
            return httpx.Response(200, text=text("hn/stories.json"))
        return httpx.Response(200, text=text("hn/comments.json"))

    async with make_client(handler, fake_time) as http:
        result = await hn_collector(sources, geo, http).run()
    assert len(result.items) == 3
    comments_request = requests[-1]
    assert (
        comments_request.url.params["tags"] == "comment,story_90000001"
    )  # not "wants to be hired"
    assert comments_request.url.params["hitsPerPage"] == "1000"


# ---------------------------------------------------------------- registry + persistence
def test_build_collectors_names_and_aliases(geo: GeoResolver) -> None:
    http = offline_http()
    default = build_collectors(http, config_dir=CONFIG_DIR, geo=geo)
    assert [c.name for c in default] == [
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "rippling",
        "breezyhr",
        "recruitee",
        "zohorecruit",
        "funding_news",
        "thub",
        "iitm",
        "nsrcel",
        "yc_india",
        "himalayas",
        "remotive",
        "internshala",
        "cutshort",
        "instahyre",
        "hn_hiring",
    ]
    (entrackr_only,) = build_collectors(http, ["entrackr"], config_dir=CONFIG_DIR, geo=geo)
    assert isinstance(entrackr_only, FundingNewsCollector)
    assert {f.name for f in entrackr_only.feeds} == {"entrackr"}
    with pytest.raises(ValueError, match="unknown collectors"):
        build_collectors(http, ["linkedin"], config_dir=CONFIG_DIR, geo=geo)


async def test_leads_persist_and_link_startups(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime, repo: Repository
) -> None:
    lab32 = next(i for i in sources.incubators if i.name == "thub_lab32")
    pages = {"/lab-32-directory/": text("thub/lab32_directory.html")}
    async with make_client(site_handler(pages, []), fake_time) as http:
        collector = ThubCollector(
            http, incubators=[lab32], geo=geo, non_company_hosts=sources.non_company_hosts
        )
        _, (report,) = await collect_and_store([collector], repo)
        await collect_and_store([collector], repo)  # idempotent
    assert (report.leads, report.startups, report.failed) == (6, 4, 0)
    assert repo.counts()["leads"] == 6
    assert repo.counts()["startups"] == 4  # only rows with a real website become startups
    lead = repo.get_lead_by_name("ExampleLabs Technologies")
    assert lead is not None
    assert lead.startup_id == repo.get_startup_by_domain("examplelabs.example").id  # type: ignore[union-attr]
