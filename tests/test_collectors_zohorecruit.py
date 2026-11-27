"""Zoho Recruit portal collector (ADR 008): embedded-JSON parsing, India filter, robots.txt."""

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.collectors.config import AtsSourceConfig, CompanySeed, SourcesConfig
from src.collectors.registry import discovered_seeds
from src.collectors.zohorecruit import ZohoRecruitCollector, embedded_jobs
from src.enrichment.ats_discovery import analyze_site, find_ats
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.enrichment.site import SitePage
from src.models import CITY_REMOTE_INDIA, AtsProvider, CollectedOpportunity, Startup, WorkMode
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

PAGE = FIXTURES / "zohorecruit" / "careers_examplecorp.html"
SEED = CompanySeed(
    name="ExampleCorp", slug="examplecorp.zohorecruit.in", domain="examplecorp.example"
)
ENDPOINT = "https://{slug}/jobs/Careers"


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


def collector(http: Any, sources: SourcesConfig, geo: GeoResolver, seeds=(SEED,)):
    return ZohoRecruitCollector(
        http,
        source=AtsSourceConfig(endpoint=ENDPOINT, companies=list(seeds)),
        geo=geo,
        matcher=InternshipMatcher.from_config(sources.internship),
    )


def raw(path: Path = PAGE) -> dict[str, str]:
    return {"url": "https://examplecorp.zohorecruit.in/jobs/Careers", "html": path.read_text()}


def test_embedded_jobs_reads_escaped_json() -> None:
    jobs = embedded_jobs(PAGE.read_text())
    assert len(jobs) == 6
    assert jobs[0]["Posting_Title"] == "Software Engineering Intern"
    assert embedded_jobs((FIXTURES / "zohorecruit" / "careers_empty.html").read_text()) == []
    with pytest.raises(ValueError, match="not a Zoho Recruit careers page"):
        embedded_jobs("<html><body>No jobs input</body></html>")
    with pytest.raises(ValueError, match="malformed jobs JSON"):
        embedded_jobs('<input id="jobs" value="[{oops">')


def test_parse_keeps_published_indian_internships(
    sources: SourcesConfig, geo: GeoResolver, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        items = list(collector(None, sources, geo).parse(raw(), SEED))
    by_title = {i.opportunity.title: i for i in items}
    # Senior role (not intern), Austin (not India), unpublished and malformed postings dropped.
    assert set(by_title) == {"Software Engineering Intern", "Marketing Intern"}
    assert "collector.posting_skipped" in caplog.text

    swe = by_title["Software Engineering Intern"].opportunity
    assert swe.url == "https://examplecorp.zohorecruit.in/jobs/Careers/900000000000100001"
    assert swe.city_canonical == "bengaluru"
    assert (swe.stipend_inr_min, swe.duration_months, swe.has_ppo) == (25000, 6, True)
    assert swe.posted_at is not None
    assert swe.posted_at.year == 2026

    marketing = by_title["Marketing Intern"].opportunity
    assert marketing.city_canonical == CITY_REMOTE_INDIA
    assert marketing.work_mode is WorkMode.REMOTE_INDIA
    assert marketing.is_paid is False

    startup = by_title["Marketing Intern"].startup
    assert startup is not None
    assert (startup.ats_provider, startup.ats_slug) == (
        AtsProvider.ZOHORECRUIT,
        "examplecorp.zohorecruit.in",
    )


async def test_run_respects_robots_and_sets_close_scope(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /recruit/Login\n")
        return httpx.Response(200, text=PAGE.read_text())

    async with make_client(handler, fake_time) as http:
        result = await collector(http, sources, geo).run()
    assert paths == ["/robots.txt", "/jobs/Careers"]  # HTML page: robots.txt checked first
    assert len(result.items) == 2
    assert all(isinstance(i, CollectedOpportunity) for i in result.items)
    assert result.closable_scopes == ["examplecorp.example"]


async def test_robots_disallow_is_an_issue_not_a_workaround(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        return httpx.Response(200, text=PAGE.read_text())

    async with make_client(handler, fake_time) as http:
        result = await collector(http, sources, geo).run()
    assert paths == ["/robots.txt"]
    assert result.items == []
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "RobotsDisallowedError")]


async def test_non_zoho_slug_is_refused_without_a_request(
    sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="")

    bad = CompanySeed(name="Evil", slug="evil.example", domain="evil.example")
    async with make_client(handler, fake_time) as http:
        result = await collector(http, sources, geo, seeds=[bad]).run()
    assert seen == []
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "ValueError")]


def test_config_rejects_non_host_zoho_slugs() -> None:
    data = SourcesConfig.load(CONFIG_DIR).model_dump(mode="json")
    data["ats"]["zohorecruit"]["companies"] = [
        {"name": "Bad", "slug": "examplecorp", "domain": "examplecorp.example"}
    ]
    with pytest.raises(ValueError, match="portal hosts"):
        SourcesConfig.model_validate(data)


def test_real_zoho_seeds_are_hosts_and_verified() -> None:
    seeds = SourcesConfig.load(CONFIG_DIR).ats[AtsProvider.ZOHORECRUIT].companies
    assert len(seeds) >= 5
    assert all(s.verified_on is not None for s in seeds)
    assert all(s.slug.endswith((".zohorecruit.in", ".zohorecruit.com")) for s in seeds)


def test_discovery_records_full_portal_host_and_auto_enrolls() -> None:
    markup = '<a href="https://examplecorp.zohorecruit.in/jobs/Careers">Jobs</a>'
    assert find_ats(markup, enabled_providers=["zohorecruit"]) == [
        ("zohorecruit", "examplecorp.zohorecruit.in")
    ]
    url = "https://examplecorp.example/careers"
    page = SitePage(path="/careers", url=url, final_url=url, status=200, html=markup)
    found = analyze_site(
        [page], cold_pitch_phrases=[], ats_embed_hosts={"zohorecruit": ["zohorecruit.in"]}
    )
    assert found.primary_tier1 is not None
    assert found.primary_tier1.pollable

    startups = [
        Startup(name="ExampleCorp", canonical_domain="examplecorp.example",
                ats_provider=AtsProvider.ZOHORECRUIT, ats_slug="examplecorp.zohorecruit.in"),
        Startup(name="OldRow", canonical_domain="oldrow.example",
                ats_provider=AtsProvider.ZOHORECRUIT, ats_slug="oldrow"),  # pre-v0.2 subdomain
        Startup(name="KekaCo", canonical_domain="kekaco.example",
                ats_provider=AtsProvider.KEKA, ats_slug="kekaco"),  # recorded, never polled
    ]  # fmt: skip
    seeds = discovered_seeds(startups)
    assert set(seeds) == {AtsProvider.ZOHORECRUIT}
    assert [s.slug for s in seeds[AtsProvider.ZOHORECRUIT]] == ["examplecorp.zohorecruit.in"]
