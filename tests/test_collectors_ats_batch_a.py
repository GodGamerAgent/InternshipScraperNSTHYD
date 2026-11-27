"""Batch A (ADR 009): Rippling, BreezyHR, Recruitee collectors; SmartRecruiters detection-only."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from src import cli
from src.collectors.ats_base import AtsCollector
from src.collectors.breezyhr import BreezyHrCollector
from src.collectors.config import AtsSourceConfig, CompanySeed, SourcesConfig
from src.collectors.recruitee import RecruiteeCollector
from src.collectors.registry import ALL_COLLECTOR_NAMES, ATS_COLLECTORS, discovered_seeds
from src.collectors.rippling import RipplingCollector
from src.enrichment.ats_discovery import find_ats
from src.enrichment.ats_probe import fetch_board, snapshot_from_payload
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import CITY_REMOTE_INDIA, AtsProvider, Startup, WorkMode
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

CASES: dict[str, tuple[type[AtsCollector], Path, str]] = {
    "rippling": (RipplingCollector, FIXTURES / "rippling" / "board_examplerip.json", "examplerip"),
    "breezyhr": (
        BreezyHrCollector,
        FIXTURES / "breezyhr" / "positions_examplebreeze.json",
        "examplebreeze",
    ),
    "recruitee": (
        RecruiteeCollector,
        FIXTURES / "recruitee" / "offers_examplerecruit.json",
        "examplerecruit",
    ),
}


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


def seed(slug: str) -> CompanySeed:
    return CompanySeed(name=slug.title(), slug=slug, domain=f"{slug}.example")


def make(name: str, http: Any, sources: SourcesConfig, geo: GeoResolver, seeds=None):
    cls, _, slug = CASES[name]
    provider = cls.provider
    return cls(
        http,
        source=AtsSourceConfig(
            endpoint=sources.ats[provider].endpoint, companies=list(seeds or [seed(slug)])
        ),
        geo=geo,
        matcher=InternshipMatcher.from_config(sources.internship),
    )


def parsed(name: str, sources: SourcesConfig, geo: GeoResolver) -> dict[str, Any]:
    _, path, slug = CASES[name]
    items = make(name, None, sources, geo).parse(json.loads(path.read_text()), seed(slug))
    return {i.opportunity.title: i.opportunity for i in items}


# ---------------------------------------------------------------- parsers
def test_rippling_parse(
    sources: SourcesConfig, geo: GeoResolver, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        jobs = parsed("rippling", sources, geo)
    assert set(jobs) == {"Software Engineering Intern", "Product Design Intern"}
    assert "collector.posting_skipped" in caplog.text  # posting without url
    swe = jobs["Software Engineering Intern"]
    assert swe.city_canonical == "bengaluru"
    assert (
        swe.url == "https://ats.rippling.com/examplerip/jobs/00000000-0000-4000-8000-000000000001"
    )
    design = jobs["Product Design Intern"]
    assert design.city_canonical == CITY_REMOTE_INDIA
    assert design.work_mode is WorkMode.REMOTE_INDIA


def test_breezyhr_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    jobs = parsed("breezyhr", sources, geo)
    assert set(jobs) == {"Marketing Intern", "Engineering Intern"}  # FT + London dropped
    marketing = jobs["Marketing Intern"]
    assert marketing.city_canonical == "hyderabad"
    assert (marketing.stipend_inr_min, marketing.stipend_inr_max) == (15000, 20000)
    assert marketing.posted_at is not None
    assert marketing.posted_at.month == 11
    assert jobs["Engineering Intern"].work_mode is WorkMode.REMOTE_INDIA


def test_recruitee_parse(sources: SourcesConfig, geo: GeoResolver) -> None:
    jobs = parsed("recruitee", sources, geo)
    # draft offer, full-time role and Paris internship dropped
    assert set(jobs) == {"Business Analyst Intern", "Content Intern"}
    analyst = jobs["Business Analyst Intern"]
    assert analyst.city_canonical == "delhi_ncr"
    assert (analyst.stipend_inr_min, analyst.stipend_inr_max) == (20000, 25000)  # structured
    assert (analyst.duration_months, analyst.has_ppo) == (6, True)
    assert analyst.posted_at is not None
    assert analyst.posted_at.isoformat() == "2026-11-04T19:22:00+00:00"
    assert jobs["Content Intern"].work_mode is WorkMode.REMOTE_INDIA


@pytest.mark.parametrize("name", list(CASES))
def test_unexpected_shape_raises(name: str, sources: SourcesConfig, geo: GeoResolver) -> None:
    with pytest.raises(ValueError, match="expected"):
        make(name, None, sources, geo).postings("not json")


# ---------------------------------------------------------------- run(): robots respected
def handler_for(name: str, paths: list[str], robots: str = "User-agent: *\nDisallow: /admin\n"):
    _, path, slug = CASES[name]

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if slug in str(request.url):
            return httpx.Response(200, content=path.read_bytes())
        return httpx.Response(404, json={"error": "not found"})

    return handler


@pytest.mark.parametrize("name", list(CASES))
async def test_run_checks_robots_isolates_missing_boards_and_sets_scope(
    name: str, sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    paths: list[str] = []
    _, _, slug = CASES[name]
    seeds = [seed(slug), seed("goneboard")]
    async with make_client(handler_for(name, paths), fake_time) as http:
        result = await make(name, http, sources, geo, seeds).run()
    assert result.targets_total == 2
    assert len(result.items) == 2
    assert [(i.target, i.stage, i.status) for i in result.issues] == [("goneboard", "fetch", 404)]
    assert any(p.endswith("/robots.txt") for p in paths)  # ADR 009: robots.txt respected
    assert result.closable_scopes == [f"{slug}.example"]


@pytest.mark.parametrize("name", list(CASES))
async def test_robots_disallow_blocks_the_board(
    name: str, sources: SourcesConfig, geo: GeoResolver, fake_time: FakeTime
) -> None:
    paths: list[str] = []
    async with make_client(
        handler_for(name, paths, "User-agent: *\nDisallow: /\n"), fake_time
    ) as http:
        result = await make(name, http, sources, geo).run()
    assert all(p.endswith("/robots.txt") for p in paths)
    assert result.items == []
    assert [(i.stage, i.error_type) for i in result.issues] == [("fetch", "RobotsDisallowedError")]


# ---------------------------------------------------------------- wiring
def test_registry_config_and_seeds(sources: SourcesConfig) -> None:
    for name in CASES:
        assert name in ATS_COLLECTORS
        assert name in ALL_COLLECTOR_NAMES
        provider = AtsProvider(name)
        assert provider in sources.ats_probe.providers
        assert sources.ats_search.queries[provider]
        assert name in sources.ats_embed_hosts
        companies = sources.ats[provider].companies
        assert companies
        assert all(c.verified_on is not None for c in companies)
    assert AtsProvider.SMARTRECRUITERS not in sources.ats  # robots-disallowed API (ADR 009)
    assert "smartrecruiters" not in ATS_COLLECTORS


def test_config_rejects_smartrecruiters_polling() -> None:
    data = SourcesConfig.load(CONFIG_DIR).model_dump(mode="json")
    data["ats"]["smartrecruiters"] = {
        "endpoint": "https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        "companies": [],
    }
    with pytest.raises(ValueError, match="without a collector"):
        SourcesConfig.model_validate(data)


def test_discovery_patterns_and_auto_enrollment() -> None:
    markup = """
      <a href="https://ats.rippling.com/examplerip/jobs">Jobs</a>
      <a href="https://examplebreeze.breezy.hr/p/abc-intern">Intern</a>
      <iframe src="https://examplerecruit.recruitee.com/o/intern"></iframe>
      <a href="https://jobs.smartrecruiters.com/ExampleSmart/123-intern">SR</a>
      <a href="https://app.breezy.hr/signin">not a board</a>
    """
    found = find_ats(
        markup, enabled_providers=["rippling", "breezyhr", "recruitee", "smartrecruiters"]
    )
    assert found == [
        ("rippling", "examplerip"),
        ("breezyhr", "examplebreeze"),
        ("recruitee", "examplerecruit"),
        ("smartrecruiters", "examplesmart"),
    ]
    startups = [
        Startup(name="R", canonical_domain="r.example",
                ats_provider=AtsProvider.RECRUITEE, ats_slug="examplerecruit"),
        Startup(name="S", canonical_domain="s.example",
                ats_provider=AtsProvider.SMARTRECRUITERS, ats_slug="examplesmart"),
    ]  # fmt: skip
    assert set(discovered_seeds(startups)) == {
        AtsProvider.RECRUITEE
    }  # SmartRecruiters never polled


def test_probe_snapshots_read_board_names() -> None:
    breezy = json.loads(CASES["breezyhr"][1].read_text())
    recruitee = json.loads(CASES["recruitee"][1].read_text())
    assert (
        snapshot_from_payload("breezyhr", "examplebreeze", breezy).company_name == "ExampleBreeze"
    )
    snap = snapshot_from_payload("recruitee", "examplerecruit", recruitee)
    assert (snap.company_name, snap.job_count) == ("ExampleRecruit", 5)
    assert snap.board_url == "https://examplerecruit.recruitee.com/"


async def test_probe_respects_robots_for_new_providers(fake_time: FakeTime) -> None:
    paths: list[str] = []
    async with make_client(
        handler_for("recruitee", paths, "User-agent: *\nDisallow: /api/\n"), fake_time
    ) as http:
        snapshot = await fetch_board(
            http, "recruitee", "examplerecruit", "https://{slug}.recruitee.com/api/offers/"
        )
    assert snapshot is None
    assert paths == ["examplerecruit.recruitee.com/robots.txt"]  # board never requested


def test_discover_cli_accepts_new_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCRAPER_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(cli, "make_http_client", lambda s: (_ for _ in ()).throw(AssertionError))
    result = CliRunner().invoke(
        cli.app, ["discover", "--dry-run", "--provider", "breezyhr", "--cities", "pune"]
    )
    assert result.exit_code == 0, result.output
    assert "site:breezy.hr intern Pune" in result.output
