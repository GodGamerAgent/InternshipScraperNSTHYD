"""End-to-end (offline) enrichment: site -> ATS/cold-pitch -> GitHub -> contacts -> score."""

import json
from datetime import date

import httpx
import pytest

from src.collectors.config import SourcesConfig
from src.collectors.registry import build_ats_collectors, discovered_seeds
from src.enrichment.scorer import ScoringConfig
from src.models import AtsProvider, ContactSourceMethod, Startup, StartupLead
from src.pipeline import EnrichmentContext, enrich_all, enrich_startup
from src.storage import Repository
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

TODAY = date(2026, 11, 26)
DOMAIN = "examplestack.example"


class FakeMx:
    async def has_mx(self, domain: str) -> bool:
        return True


def gh(name: str):
    return json.loads((FIXTURES / "github" / name).read_text(encoding="utf-8"))


def handler_factory(seen: list[str]):
    site = {"/": "home.html", "/careers": "careers.html", "/about": "about.html"}
    users = gh("users.json")

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        seen.append(f"{host}{path}")
        if host == "broken.example":
            raise RuntimeError("simulated bug while talking to broken.example")
        if host == "api.github.com":
            if path.endswith("/public_members"):
                return httpx.Response(200, json=gh("members.json"))
            if path.endswith("/repos"):
                return httpx.Response(200, json=gh("repos.json"))
            if path.startswith("/users/"):
                return httpx.Response(200, json=users[path.rsplit("/", 1)[-1]])
            return httpx.Response(200, json=gh("org.json"))
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /team\n")
        if host == DOMAIN and path in site:
            return httpx.Response(200, text=(FIXTURES / "site" / site[path]).read_text())
        return httpx.Response(404)

    return handler


@pytest.fixture
def seeded(repo: Repository) -> Startup:
    sid = repo.upsert_startup(
        Startup(
            name="ExampleStack",
            canonical_domain=DOMAIN,
            hq_city_canonical="bengaluru",
            funding_stage="seed",
            last_funded_at=date(2026, 11, 10),
        )
    )
    repo.upsert_lead(
        StartupLead(name="ExampleStack", source="thub_lab32", domain=DOMAIN, startup_id=sid,
                    founders=["Asha Rao", "Vikram Iyer"])
    )  # fmt: skip
    startup = repo.get_startup(sid)
    assert startup is not None
    return startup


def context(http) -> EnrichmentContext:
    return EnrichmentContext(
        http=http,
        sources=SourcesConfig.load(CONFIG_DIR),
        scoring=ScoringConfig.load(CONFIG_DIR),
        resolver=FakeMx(),
        today=TODAY,
    )


async def test_enrich_startup_end_to_end(
    repo: Repository, seeded: Startup, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    async with make_client(handler_factory(seen), fake_time) as http:
        report = await enrich_startup(seeded, repo, context(http))

    assert report.errors == []
    assert report.score == 100
    assert report.label == "high"
    assert "greenhouse:examplestack" in report.ats
    assert f"{DOMAIN}/team" not in seen  # robots-disallowed page never requested

    stored = repo.get_startup(seeded.id)  # type: ignore[arg-type]
    assert stored is not None
    assert stored.hidden_score == 100
    assert (stored.ats_provider, stored.ats_slug) == (AtsProvider.GREENHOUSE, "examplestack")
    assert stored.github_org == "examplestack-oss"
    assert stored.open_to_cold_pitch is True
    assert {"go", "python"} <= set(stored.tech_stack)
    assert stored.hq_city_canonical == "bengaluru"  # untouched facts preserved

    contacts = repo.list_contacts(startup_id=seeded.id)
    by_method: dict[ContactSourceMethod, set[str]] = {}
    for c in contacts:
        by_method.setdefault(c.source_method, set()).add(c.email or c.profile_url or "")
    assert {
        "careers@examplestack.example",
        "founders@examplestack.example",
        "https://linkedin.com/in/asha-rao-example",
        "https://github.com/vikram-iyer-example",  # a person, not the org -> kept
    } <= by_method[ContactSourceMethod.DIRECT_PUBLIC]
    assert "https://github.com/examplestack-oss" not in by_method[ContactSourceMethod.DIRECT_PUBLIC]
    assert {"oss@examplestack.example", "vikram@examplestack.example"} <= by_method[
        ContactSourceMethod.GITHUB_PUBLIC
    ]
    # vikram@ was both a GitHub email (0.85) and a pattern guess (0.50): the confident one wins.
    assert by_method[ContactSourceMethod.PATTERN_MX_VERIFIED] == {
        "asha@examplestack.example",
        "asha.rao@examplestack.example",
        "vikram.iyer@examplestack.example",
    }
    assert not any("gmail.com" in (c.email or "") for c in contacts)
    assert len(contacts) == report.contacts == 12


async def test_enrichment_is_idempotent(
    repo: Repository, seeded: Startup, fake_time: FakeTime
) -> None:
    async with make_client(handler_factory([]), fake_time) as http:
        await enrich_startup(seeded, repo, context(http))
        first = repo.counts()
        await enrich_startup(repo.get_startup(seeded.id), repo, context(http))  # type: ignore[arg-type]
    assert repo.counts() == first


async def test_enrich_all_isolates_failures(
    repo: Repository, seeded: Startup, fake_time: FakeTime
) -> None:
    repo.upsert_startup(Startup(name="Broken", canonical_domain="broken.example"))
    async with make_client(handler_factory([]), fake_time) as http:
        reports = {r.domain: r for r in await enrich_all(repo, context(http))}
    assert reports[DOMAIN].score == 100
    assert reports["broken.example"].score is None
    assert "RuntimeError" in reports["broken.example"].errors[0]


async def test_github_failure_is_recorded_not_fatal(
    repo: Repository, seeded: Startup, fake_time: FakeTime
) -> None:
    base = handler_factory([])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(403, json={"message": "API rate limit exceeded"})
        return base(request)

    async with make_client(handler, fake_time) as http:
        report = await enrich_startup(seeded, repo, context(http))
    assert any(e.startswith("github:") for e in report.errors)
    assert report.score == 65  # 30 funding + 20 location + 15 cold pitch; no GitHub, no stack
    assert not any("GitHub" in reason for reason in report.reasons)


async def test_discovered_boards_are_auto_enrolled(
    repo: Repository, seeded: Startup, fake_time: FakeTime
) -> None:
    async with make_client(handler_factory([]), fake_time) as http:
        await enrich_startup(seeded, repo, context(http))
        seeds = discovered_seeds(repo.list_startups())
        (greenhouse,) = build_ats_collectors(
            http, ["greenhouse"], config_dir=CONFIG_DIR, extra_seeds=seeds
        )
    slugs = [c.slug for c in greenhouse.source.companies]
    assert "examplestack" in slugs
    assert "groww" in slugs  # YAML seeds kept
    enrolled = next(c for c in greenhouse.source.companies if c.slug == "examplestack")
    assert enrolled.hq_city == "bengaluru"
