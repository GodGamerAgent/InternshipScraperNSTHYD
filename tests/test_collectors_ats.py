"""Offline tests for the Tier 1 ATS collectors, driven by anonymized fixtures."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.collectors.ashby import AshbyCollector
from src.collectors.ats_base import AtsCollector
from src.collectors.breezyhr import BreezyHrCollector
from src.collectors.config import AtsSourceConfig, CompanySeed, SourcesConfig
from src.collectors.greenhouse import GreenhouseCollector
from src.collectors.lever import LeverCollector
from src.collectors.recruitee import RecruiteeCollector
from src.collectors.registry import ATS_COLLECTORS, build_ats_collectors
from src.collectors.rippling import RipplingCollector
from src.collectors.workable import WorkableCollector
from src.collectors.zohorecruit import ZohoRecruitCollector
from src.core.http import HttpClient
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import (
    CITY_REMOTE_INDIA,
    AtsProvider,
    CollectedOpportunity,
    Country,
    WorkMode,
)
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

SEEDS = {
    "greenhouse": CompanySeed(
        name="ExampleLabs", slug="examplelabs", domain="examplelabs.example", hq_city="hyderabad"
    ),
    "lever": CompanySeed(
        name="ExamplePay", slug="examplepay", domain="examplepay.example", hq_city="bengaluru"
    ),
    "ashby": CompanySeed(
        name="ExampleMind", slug="examplemind", domain="examplemind.example", hq_city="bengaluru"
    ),
    "workable": CompanySeed(
        name="ExampleWorks", slug="exampleworks", domain="exampleworks.example", hq_city="chennai"
    ),
}
FIXTURE_FILES = {
    "greenhouse": FIXTURES / "greenhouse" / "board_examplelabs.json",
    "lever": FIXTURES / "lever" / "postings_examplepay.json",
    "ashby": FIXTURES / "ashby" / "board_examplemind.json",
    "workable": FIXTURES / "workable" / "account_exampleworks.json",
}


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


def load(name: str) -> Any:
    return json.loads(FIXTURE_FILES[name].read_text(encoding="utf-8"))


def make_collector(
    cls: type[AtsCollector],
    http: HttpClient,
    sources: SourcesConfig,
    geo: GeoResolver,
    seeds: list[CompanySeed] | None = None,
    **kwargs: Any,
) -> AtsCollector:
    source = AtsSourceConfig(
        endpoint=sources.ats[cls.provider].endpoint,
        companies=seeds if seeds is not None else [SEEDS[cls.name]],
    )
    matcher = InternshipMatcher.from_config(sources.internship)
    return cls(http, source=source, geo=geo, matcher=matcher, **kwargs)


def parse(cls: type[AtsCollector], sources: SourcesConfig, geo: GeoResolver, **kw: Any):
    http = HttpClient(user_agent="india-internship-scraper/0.1")  # parse() never uses it
    collector = make_collector(cls, http, sources, geo, **kw)
    return list(collector.parse(load(cls.name), SEEDS[cls.name]))


def by_title(items: list[CollectedOpportunity]) -> dict[str, Any]:
    return {item.opportunity.title: item.opportunity for item in items}


# ---------------------------------------------------------------- Greenhouse
def test_greenhouse_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    items = parse(GreenhouseCollector, sources, geo)
    opps = by_title(items)
    assert list(opps) == ["Software Engineering Intern", "Product Design", "Growth Intern"]

    swe = opps["Software Engineering Intern"]
    assert (swe.city_canonical, swe.work_mode) == ("bengaluru", WorkMode.ONSITE)
    assert swe.raw_location == "Onsite in Bangalore"
    assert (swe.stipend_inr_min, swe.stipend_inr_max, swe.is_paid) == (40000, 40000, True)
    assert swe.duration_months == 6
    assert swe.has_ppo is True
    assert swe.posted_at == datetime(2026, 11, 1, 4, 30, tzinfo=UTC)
    assert swe.description is not None
    assert "Build APIs" in swe.description
    assert "<" not in swe.description  # double-escaped HTML fully stripped
    assert swe.source == "greenhouse"
    assert swe.startup_id is None  # assigned by the pipeline

    design = opps["Product Design"]  # only the department says "Internship"
    assert design.city_canonical == "hyderabad"  # "India" -> seed HQ fallback
    assert design.is_paid is False
    assert design.duration_months == 3

    growth = opps["Growth Intern"]  # bare "Remote" at an Indian startup
    assert growth.city_canonical == CITY_REMOTE_INDIA
    assert growth.work_mode is WorkMode.REMOTE_INDIA
    assert growth.is_paid is True


def test_greenhouse_skips_malformed_posting_and_logs(
    sources: SourcesConfig, geo: GeoResolver, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    parse(GreenhouseCollector, sources, geo)
    skipped = [r for r in caplog.records if r.getMessage() == "collector.posting_skipped"]
    assert len(skipped) == 1
    assert skipped[0].error_type == "KeyError"


def test_foreign_postings_kept_when_india_only_disabled(
    sources: SourcesConfig, geo: GeoResolver
) -> None:
    opps = by_title(parse(GreenhouseCollector, sources, geo, india_only=False))
    kl = opps["Data Analyst Intern"]
    assert kl.country is Country.GLOBAL  # Kuala Lumpur never falls back to the Indian HQ
    assert not kl.stipend_inr_min  # MYR is not INR


def test_startup_built_from_seed(sources: SourcesConfig, geo: GeoResolver) -> None:
    startup = parse(GreenhouseCollector, sources, geo)[0].startup
    assert startup is not None
    assert startup.canonical_domain == "examplelabs.example"
    assert startup.ats_provider is AtsProvider.GREENHOUSE
    assert startup.ats_slug == "examplelabs"
    assert (startup.hq_city_canonical, startup.hq_state) == ("hyderabad", "Telangana")
    assert startup.hq_lat is not None
    assert startup.discovered_via == "greenhouse"


# ---------------------------------------------------------------- Lever
def test_lever_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    opps = by_title(parse(LeverCollector, sources, geo))
    assert list(opps) == ["Intern - Product Analytics", "Backend Engineer"]

    analytics = opps["Intern - Product Analytics"]
    assert analytics.city_canonical == "bengaluru"
    assert analytics.work_mode is WorkMode.HYBRID
    assert analytics.raw_location == "Bangalore - WF / Hyderabad"
    assert (analytics.stipend_inr_min, analytics.stipend_inr_max) == (25000, 30000)  # structured
    assert analytics.duration_months == 6
    assert analytics.has_ppo is True  # from the "Perks" list
    assert analytics.posted_at is not None

    backend = opps["Backend Engineer"]  # commitment "Intern" makes it an internship
    assert backend.city_canonical == CITY_REMOTE_INDIA
    assert (backend.stipend_inr_min, backend.stipend_inr_max) == (20000, 30000)  # text fallback
    assert backend.duration_months is None  # "0-6 months of experience" is not a duration


# ---------------------------------------------------------------- Ashby
def test_ashby_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    opps = by_title(parse(AshbyCollector, sources, geo))
    assert list(opps) == ["Marketing internship (brand & communication)", "ML Research Intern"]

    marketing = opps["Marketing internship (brand & communication)"]  # employmentType FullTime
    assert marketing.city_canonical == "bengaluru"
    assert marketing.raw_location == "Bengaluru / Mumbai"
    assert (marketing.stipend_inr_min, marketing.stipend_inr_max) == (30000, 40000)

    research = opps["ML Research Intern"]
    assert research.work_mode is WorkMode.REMOTE_INDIA
    assert research.duration_months == 2.8  # 12 weeks
    assert research.has_ppo is False
    assert research.description == "Duration: 12 weeks.\nPlease note: no PPO for this cohort."


# ---------------------------------------------------------------- Workable
def test_workable_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    opps = by_title(parse(WorkableCollector, sources, geo))
    assert list(opps) == ["Frontend Developer Intern", "QA Trainee"]

    frontend = opps["Frontend Developer Intern"]
    assert (frontend.city_canonical, frontend.state) == ("chennai", "Tamil Nadu")
    assert (frontend.stipend_inr_min, frontend.stipend_inr_max) == (15000, 20000)
    assert frontend.duration_months == 3
    assert frontend.posted_at == datetime(2026, 11, 10, tzinfo=UTC)

    qa = opps["QA Trainee"]  # hidden location -> top-level fields; telecommuting -> remote
    assert qa.city_canonical == "pune"
    assert qa.work_mode is WorkMode.REMOTE_INDIA
    assert qa.is_paid is True


# ---------------------------------------------------------------- run() over HTTP
def fixture_handler(paths: list[str]):
    """Serve fixtures for the ``example*`` slugs; 404 for anything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        for name, seed in SEEDS.items():
            if seed.slug in request.url.path:
                return httpx.Response(200, content=FIXTURE_FILES[name].read_bytes())
        return httpx.Response(404, json={"ok": False, "error": "Document not found"})

    return handler


# Tier 1 JSON-API collectors only; Zoho Recruit (HTML, robots respected) has its own tests.
JSON_ATS = {name: cls for name, cls in ATS_COLLECTORS.items() if name in FIXTURE_FILES}


@pytest.mark.parametrize("cls", list(JSON_ATS.values()), ids=list(JSON_ATS))
async def test_run_isolates_missing_boards_and_skips_robots(
    cls: type[AtsCollector], sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    paths: list[str] = []
    seeds = [SEEDS[cls.name], CompanySeed(name="Gone", slug="gone-board", domain="gone.example")]
    async with make_client(fixture_handler(paths), fake_time) as http:
        result = await make_collector(cls, http, sources, geo, seeds=seeds).run()

    assert result.targets_total == 2
    assert result.items  # the good board still produced internships
    assert all(isinstance(i, CollectedOpportunity) for i in result.items)
    assert [(i.target, i.stage, i.status) for i in result.issues] == [("gone-board", "fetch", 404)]
    assert "/robots.txt" not in paths  # documented public API (ADR 002)


async def test_unexpected_payload_shape_is_a_parse_issue(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "board"])

    async with make_client(handler, fake_time) as http:
        result = await make_collector(GreenhouseCollector, http, sources, geo).run()
    assert [(i.stage, i.error_type) for i in result.issues] == [("parse", "ValueError")]


async def test_slug_is_url_encoded(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"jobs": []})

    seed = CompanySeed(name="Dots", slug="acme.labs_in", domain="acme.example")
    async with make_client(handler, fake_time) as http:
        await make_collector(GreenhouseCollector, http, sources, geo, seeds=[seed]).run()
    assert seen == ["https://boards-api.greenhouse.io/v1/boards/acme.labs_in/jobs?content=true"]


# ---------------------------------------------------------------- registry
def test_registry_builds_all_or_subset(geo: GeoResolver, tmp_path: Path) -> None:
    http = HttpClient(user_agent="india-internship-scraper/0.1")
    every = build_ats_collectors(http, config_dir=CONFIG_DIR, geo=geo)
    assert [c.name for c in every] == [
        "greenhouse", "lever", "ashby", "workable", "rippling", "breezyhr", "recruitee",
        "zohorecruit",
    ]  # fmt: skip
    assert {type(c) for c in every} == {
        GreenhouseCollector,
        LeverCollector,
        AshbyCollector,
        WorkableCollector,
        RipplingCollector,
        BreezyHrCollector,
        RecruiteeCollector,
        ZohoRecruitCollector,
    }
    subset = build_ats_collectors(http, ["ashby"], config_dir=CONFIG_DIR, geo=geo)
    assert [c.name for c in subset] == ["ashby"]
    with pytest.raises(ValueError, match="unknown ATS collectors"):
        build_ats_collectors(http, ["linkedin"], config_dir=CONFIG_DIR, geo=geo)
    sources = SourcesConfig.load(CONFIG_DIR)
    tier1_only = sources.model_copy(
        update={"ats": {p: c for p, c in sources.ats.items() if p is not AtsProvider.ZOHORECRUIT}}
    )
    assert "zohorecruit" not in [
        c.name for c in build_ats_collectors(http, sources=tier1_only, geo=geo)
    ]
    with pytest.raises(ValueError, match=r"no sources\.yaml 'ats' section"):
        build_ats_collectors(http, ["zohorecruit"], sources=tier1_only, geo=geo)
