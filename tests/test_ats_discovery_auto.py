"""Offline tests for automatic ATS seed discovery (ADR 007): probing, anti-collision, search."""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import src.cli as cli
from src.collectors.ats_search import build_queries, discover_boards, extract_candidates
from src.collectors.config import SourcesConfig
from src.core.config import Settings
from src.core.firecrawl_client import FirecrawlClient
from src.core.http import FetchError
from src.core.search import (
    BraveSearch,
    FirecrawlSearch,
    SearchHit,
    search_backend_from_settings,
    search_backend_name,
)
from src.enrichment.ats_probe import (
    BoardSnapshot,
    board_matches,
    candidate_slugs,
    fetch_board,
    infer_domain,
    probe_startup,
    registrable_domain,
    snapshot_from_payload,
)
from src.enrichment.scorer import ScoringConfig
from src.models import AtsProvider, Startup
from src.pipeline import EnrichmentContext, enrich_startup, run_discover
from src.storage import Repository
from src.ui import jobs
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

SEARCH = FIXTURES / "ats_search"
NOISE = ("careers", "jobs", "hiring", "pvt", "ltd", "technologies", "labs")


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


@pytest.fixture(scope="module")
def endpoints(sources: SourcesConfig) -> dict[str, str]:
    return {p.value: c.endpoint for p, c in sources.ats.items()}


# ---------------------------------------------------------------- slug generation
@pytest.mark.parametrize(
    ("domain", "name", "slugs"),
    [
        ("groww.in", "Groww", ["groww"]),
        ("getacme.in", "Acme Labs Pvt Ltd", ["getacme", "acme", "acmelabs"]),
        ("careers.acme.co.in", "Acme", ["acme"]),
        ("hydstack.example", "Hyd Stack", ["hydstack", "hyd-stack"]),
        ("app.io", "App", []),  # too short / generic: would collide with other boards
        ("jobs.example", "Jobs", []),  # stopword
        ("zo.in", "Zo", []),
    ],
)
def test_candidate_slugs(sources: SourcesConfig, domain: str, name: str, slugs: list[str]) -> None:
    assert candidate_slugs(domain, name, sources.ats_probe) == slugs


def test_registrable_domain() -> None:
    assert registrable_domain("careers.acme.co.in") == "acme.co.in"
    assert registrable_domain("jobs.acme.io") == "acme.io"
    assert registrable_domain("acme.in") == "acme.in"


# ---------------------------------------------------------------- snapshots & anti-collision
def gh_payload(company: str, link: str | None = None) -> dict[str, Any]:
    content = (
        f"&lt;p&gt;About us: &lt;a href=&quot;{link}&quot;&gt;site&lt;/a&gt;&lt;/p&gt;"
        if link
        else ""
    )
    return {"jobs": [{"title": "Intern", "company_name": company, "content": content,
                      "absolute_url": "https://job-boards.greenhouse.io/x/jobs/1"}]}  # fmt: skip


def test_snapshot_extracts_name_count_and_links() -> None:
    snap = snapshot_from_payload("greenhouse", "hydstack", gh_payload("HydStack Careers",
                                 "https://www.hydstack.example/about"))  # fmt: skip
    assert snap.company_name == "HydStack Careers"
    assert snap.job_count == 1
    assert "https://www.hydstack.example/about" in snap.urls  # HTML-escaped content unescaped
    assert snap.board_url == "https://job-boards.greenhouse.io/hydstack"
    workable = snapshot_from_payload("workable", "wk", {"name": "WorkCo", "jobs": []})
    assert (workable.company_name, workable.job_count) == ("WorkCo", 0)
    lever = snapshot_from_payload("lever", "lv", [{"text": "Intern", "descriptionPlain": "x"}])
    assert (lever.company_name, lever.job_count) == (None, 1)


@pytest.mark.parametrize(
    ("snapshot", "domain", "name", "expected"),
    [
        # link to the startup's own domain (any subdomain) -> accepted
        (BoardSnapshot("lever", "hydstack", None, 2, ("https://careers.hydstack.example/x",)),
         "hydstack.example", "HydStack", True),
        # board display name matches (noise words ignored) -> accepted
        (BoardSnapshot("greenhouse", "hydstack", "HydStack Careers", 3, ()),
         "hydstack.example", "HydStack Technologies Pvt Ltd", True),
        (BoardSnapshot("greenhouse", "razorpaysoftwareprivatelimited",
                       "Razorpay Software Private Limited", 5, ()),
         "razorpay.com", "Razorpay", True),
        # same slug, someone else's board (different name, links elsewhere) -> REJECTED
        (BoardSnapshot("greenhouse", "atlan", "Atlan Travel Group", 3,
                       ("https://atlantravel.example/",)), "atlan.example", "Atlan", False),
        (BoardSnapshot("ashby", "atlan", None, 8, ("https://atlan.com/careers",)),
         "atlan.in", "Atlan Travels", False),
        # no evidence at all (empty board, no name) -> REJECTED
        (BoardSnapshot("ashby", "hydstack", None, 0, ()), "hydstack.example", "HydStack", False),
    ],
)  # fmt: skip
def test_board_matches_anti_collision(sources, snapshot, domain, name, expected) -> None:
    noise = sources.ats_probe.name_noise  # the real configured noise words
    assert board_matches(snapshot, domain=domain, name=name, name_noise=noise) is expected


def test_infer_domain(sources: SourcesConfig) -> None:
    snap = BoardSnapshot(
        "lever", "chaiops", None, 2,
        ("https://www.linkedin.com/company/chaiops", "https://jobs.lever.co/chaiops/1",
         "https://chaiops.example/about", "https://blog.chaiops.example/post",
         "https://unrelated-vendor.example/"),
    )  # fmt: skip
    assert infer_domain(snap, non_company_hosts=sources.non_company_hosts) == "chaiops.example"
    unrelated = BoardSnapshot("lever", "chaiops", None, 1, ("https://unrelated-vendor.example/",))
    assert infer_domain(unrelated, non_company_hosts=sources.non_company_hosts) is None
    by_name = BoardSnapshot("greenhouse", "cos-123", "Chai Ops Careers", 1,
                            ("https://chaiops.example/",))  # fmt: skip
    assert infer_domain(by_name, non_company_hosts=[], name_noise=NOISE) == "chaiops.example"


# ---------------------------------------------------------------- live-API fetching (mocked)
def ats_handler(boards: dict[str, httpx.Response], seen: list[str]):
    """Serve ATS JSON by URL path; anything else 404 (Lever-style body for lever)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        for key, response in boards.items():
            if key in f"{request.url.host}{request.url.path}":
                return response
        if request.url.host == "api.lever.co":
            return httpx.Response(404, json={"ok": False, "error": "Document not found"})
        return httpx.Response(404)

    return handler


async def test_fetch_board_statuses(endpoints, fake_time: FakeTime) -> None:
    seen: list[str] = []
    boards = {
        "boards-api.greenhouse.io/v1/boards/good": httpx.Response(200, json=gh_payload("Good")),
        "boards-api.greenhouse.io/v1/boards/broken": httpx.Response(500),
        "boards-api.greenhouse.io/v1/boards/notjson": httpx.Response(200, text="<html>"),
    }
    async with make_client(ats_handler(boards, seen), fake_time) as http:
        good = await fetch_board(http, "greenhouse", "good", endpoints["greenhouse"])
        assert good is not None
        assert good.company_name == "Good"
        assert await fetch_board(http, "lever", "missing", endpoints["lever"]) is None  # 404
        assert await fetch_board(http, "greenhouse", "broken", endpoints["greenhouse"]) is None
        assert await fetch_board(http, "greenhouse", "notjson", endpoints["greenhouse"]) is None
    assert not any(path.endswith("/robots.txt") for path in seen)  # documented public APIs


async def test_probe_stops_at_first_verified_board(sources, endpoints, fake_time) -> None:
    seen: list[str] = []
    boards = {
        # greenhouse "hydstack" belongs to someone else -> rejected, probing continues
        "boards-api.greenhouse.io/v1/boards/hydstack": httpx.Response(
            200, json=gh_payload("Hyd Stack Furniture", "https://hydstackfurniture.example")
        ),
        "api.lever.co/v0/postings/hydstack": httpx.Response(
            200,
            json=[{"text": "SDE Intern", "descriptionPlain": "See https://hydstack.example/team"}],
        ),
    }
    async with make_client(ats_handler(boards, seen), fake_time) as http:
        found = await probe_startup(
            http, domain="hydstack.example", name="HydStack", endpoints=endpoints,
            rules=sources.ats_probe, providers=["greenhouse", "lever", "ashby", "workable"],
        )  # fmt: skip
    assert found is not None
    assert (found.provider, found.slug) == ("lever", "hydstack")
    assert not any("ashbyhq" in s or "workable" in s for s in seen)  # stopped after verification


async def test_probe_returns_none_when_nothing_matches(sources, endpoints, fake_time) -> None:
    seen: list[str] = []
    async with make_client(ats_handler({}, seen), fake_time) as http:
        found = await probe_startup(
            http, domain="nowhere.example", name="Nowhere", endpoints=endpoints,
            rules=sources.ats_probe, providers=["greenhouse", "lever"],
        )  # fmt: skip
    assert found is None
    assert len(seen) == 2  # one candidate slug x two providers


# ---------------------------------------------------------------- probing inside `enrich`
class FakeMx:
    async def has_mx(self, domain: str) -> bool:
        return False


async def test_enrich_probes_and_stores_board(repo: Repository, fake_time: FakeTime) -> None:
    sid = repo.upsert_startup(Startup(name="HydStack", canonical_domain="hydstack.example"))
    lever = httpx.Response(
        200, json=[{"text": "Intern", "descriptionPlain": "https://hydstack.example"}]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.host == "hydstack.example" and request.url.path == "/":
            return httpx.Response(200, text="<html><body><p>We build tools.</p></body></html>")
        if request.url.host == "api.lever.co" and request.url.path.endswith("/hydstack"):
            return lever
        return httpx.Response(404)

    async with make_client(handler, fake_time) as http:
        ctx = EnrichmentContext(http=http, sources=SourcesConfig.load(CONFIG_DIR),
                                scoring=ScoringConfig.load(CONFIG_DIR), resolver=FakeMx(),
                                today=date(2026, 11, 26))  # fmt: skip
        report = await enrich_startup(repo.get_startup(sid), repo, ctx)  # type: ignore[arg-type]
    stored = repo.get_startup(sid)
    assert stored is not None
    assert (stored.ats_provider, stored.ats_slug) == (AtsProvider.LEVER, "hydstack")
    assert "lever:hydstack (probed)" in report.ats


async def test_enrich_does_not_probe_known_boards(repo: Repository, fake_time: FakeTime) -> None:
    sid = repo.upsert_startup(Startup(name="Known", canonical_domain="knownco.example",
                                      ats_provider="greenhouse", ats_slug="knownco"))  # fmt: skip
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(404)

    async with make_client(handler, fake_time) as http:
        ctx = EnrichmentContext(
            http=http,
            sources=SourcesConfig.load(CONFIG_DIR),
            scoring=ScoringConfig.load(CONFIG_DIR),
            resolver=FakeMx(),
        )
        await enrich_startup(repo.get_startup(sid), repo, ctx)  # type: ignore[arg-type]
    assert not any(h.startswith("api.") or "greenhouse" in h for h in seen)


# ---------------------------------------------------------------- search URL extraction
def hits(name: str) -> list[SearchHit]:
    data = json.loads((SEARCH / name).read_text())
    return [SearchHit(r["url"], r.get("title", "")) for r in data["web"]["results"]]


def test_extract_candidates_from_result_urls() -> None:
    found = extract_candidates(
        hits("brave.json"), providers=["greenhouse", "lever", "ashby", "workable"]
    )
    assert [(c.provider, c.slug) for c in found] == [
        ("greenhouse", "hydstack"),
        ("lever", "chaiops"),
        ("ashby", "nolink-labs"),
        ("lever", "ghostboard"),
        ("greenhouse", "groww"),
    ]  # workable /j/<code> and the blog URL carry no board slug
    only_lever = extract_candidates(hits("brave.json"), providers=["lever"])
    assert {c.slug for c in only_lever} == {"chaiops", "ghostboard"}


def test_build_queries(sources: SourcesConfig) -> None:
    pairs = build_queries(sources, providers=["lever"], city_names=["Hyderabad", "Chennai"])
    assert pairs == [
        ("lever", "site:jobs.lever.co intern Hyderabad"),
        ("lever", "site:jobs.lever.co intern Chennai"),
        ("lever", "site:jobs.lever.co intern India"),
    ]


# ---------------------------------------------------------------- search backends (mocked APIs)
async def test_brave_backend_sends_key_in_header_only(fake_time: FakeTime) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=(SEARCH / "brave.json").read_text())

    async with make_client(handler, fake_time) as http:
        results = await BraveSearch(http, "brv-secret").search("site:jobs.lever.co intern", 50)
    (request,) = requests
    assert request.headers["x-subscription-token"] == "brv-secret"
    assert "brv-secret" not in str(request.url)
    assert request.url.params["count"] == "20"  # capped to the API maximum
    assert len(results) == 7
    assert "brv-secret" not in repr(BraveSearch(http, "brv-secret"))


@pytest.mark.parametrize(("fixture", "count"), [("firecrawl_v1.json", 3), ("firecrawl_v2.json", 2)])
async def test_firecrawl_backend_parses_v1_and_v2(fake_time, fixture: str, count: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/search")
        assert json.loads(request.content)["query"] == "q"
        return httpx.Response(200, text=(SEARCH / fixture).read_text())

    async with make_client(handler, fake_time) as http:
        results = await FirecrawlSearch(FirecrawlClient(http, "fc-key")).search("q", 10)
    assert len(results) == count


async def test_firecrawl_backend_failure(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": "quota exceeded"})

    async with make_client(handler, fake_time) as http:
        with pytest.raises(FetchError, match="quota"):
            await FirecrawlSearch(FirecrawlClient(http, "fc-key")).search("q", 10)


def test_backend_selection() -> None:
    assert search_backend_name(Settings.from_env({})) is None
    assert search_backend_name(Settings.from_env({"FIRECRAWL_API_KEY": "f"})) == "firecrawl"
    both = Settings.from_env({"FIRECRAWL_API_KEY": "f", "BRAVE_SEARCH_API_KEY": "b"})
    assert search_backend_name(both) == "brave"
    http = make_client(lambda r: httpx.Response(500), FakeTime())
    assert search_backend_from_settings(http, Settings.from_env({})) is None
    assert isinstance(search_backend_from_settings(http, both), BraveSearch)


# ---------------------------------------------------------------- end-to-end discovery + SQLite
class FakeBackend:
    name = "fake"

    def __init__(self, results: list[SearchHit]) -> None:
        self.results = results
        self.queries: list[str] = []

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        self.queries.append(query)
        return self.results if len(self.queries) == 1 else []


def discovery_boards() -> dict[str, httpx.Response]:
    return {
        "boards-api.greenhouse.io/v1/boards/hydstack": httpx.Response(
            200, json=gh_payload("HydStack", "https://hydstack.example/about")
        ),
        "api.lever.co/v0/postings/chaiops": httpx.Response(
            200, json=[{"text": "DevOps Intern", "descriptionPlain": "https://chaiops.example"}]
        ),
        "api.ashbyhq.com/posting-api/job-board/nolink-labs": httpx.Response(
            200, json={"jobs": [{"title": "Intern", "descriptionPlain": "Join us"}]}
        ),
        # lever "ghostboard" is not served -> 404 -> not_found; greenhouse "groww" is a YAML seed
    }


async def test_run_discover_persists_verified_boards(repo: Repository, sources, fake_time) -> None:
    yaml_before = hashlib.sha256((CONFIG_DIR / "sources.yaml").read_bytes()).hexdigest()
    seen: list[str] = []
    backend = FakeBackend(hits("brave.json"))
    async with make_client(ats_handler(discovery_boards(), seen), fake_time) as http:
        result, report = await run_discover(
            repo, http, backend, sources, providers=["greenhouse", "lever", "ashby", "workable"],
            city_names=["Hyderabad"], limit=10,
        )  # fmt: skip
    statuses = {(r["provider"], r["slug"]): r["status"] for r in report.rows}
    assert statuses == {
        ("greenhouse", "hydstack"): "startup",
        ("lever", "chaiops"): "startup",
        ("ashby", "nolink-labs"): "lead",  # verified, but no plausible company domain
        ("lever", "ghostboard"): "not_found",
    }
    assert result.skipped_known == 1  # groww is already a verified YAML seed
    assert (report.saved_startups, report.saved_leads, report.not_found) == (2, 1, 1)

    hyd = repo.get_startup_by_domain("hydstack.example")
    assert hyd is not None
    assert (hyd.ats_provider, hyd.ats_slug, hyd.discovered_via) == (
        AtsProvider.GREENHOUSE, "hydstack", "ats_search",
    )  # fmt: skip
    lead = repo.get_lead_by_name("Nolink Labs")
    assert lead is not None
    assert lead.source_url == "https://jobs.ashbyhq.com/nolink-labs"
    assert hashlib.sha256((CONFIG_DIR / "sources.yaml").read_bytes()).hexdigest() == yaml_before

    # A second run skips everything now stored in SQLite.
    async with make_client(ats_handler(discovery_boards(), []), fake_time) as http:
        again, report2 = await run_discover(
            repo, http, FakeBackend(hits("brave.json")), sources,
            providers=["greenhouse", "lever", "ashby", "workable"], city_names=["Hyderabad"],
        )  # fmt: skip
    assert again.skipped_known == 3  # groww + hydstack + chaiops
    assert {r["slug"] for r in report2.rows} == {"nolink-labs", "ghostboard"}  # leads aren't seeds


async def test_run_discover_never_overwrites_a_different_board(
    repo: Repository, sources, fake_time
) -> None:
    repo.upsert_startup(Startup(name="HydStack", canonical_domain="hydstack.example",
                                ats_provider="lever", ats_slug="hydstack-old"))  # fmt: skip
    backend = FakeBackend([SearchHit("https://job-boards.greenhouse.io/hydstack/jobs/1")])
    async with make_client(ats_handler(discovery_boards(), []), fake_time) as http:
        _, report = await run_discover(repo, http, backend, sources, providers=["greenhouse"],
                                       city_names=["Hyderabad"])  # fmt: skip
    assert report.conflicts == 1
    stored = repo.get_startup_by_domain("hydstack.example")
    assert stored is not None
    assert (stored.ats_provider, stored.ats_slug) == (AtsProvider.LEVER, "hydstack-old")


async def test_discover_limit(repo: Repository, sources, fake_time) -> None:
    async with make_client(ats_handler(discovery_boards(), []), fake_time) as http:
        result = await discover_boards(
            http, FakeBackend(hits("brave.json")), sources,
            providers=["greenhouse", "lever", "ashby"], city_names=["Hyderabad"], limit=1,
        )  # fmt: skip
    assert len(result.boards) == 1
    assert len(result.candidates) == 5


# ---------------------------------------------------------------- CLI + UI job
runner = CliRunner()


def test_discover_without_keys_makes_no_network_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in ("BRAVE_SEARCH_API_KEY", "FIRECRAWL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SCRAPER_DB_PATH", str(tmp_path / "db.sqlite"))

    def no_network(settings):
        raise AssertionError("discover must not create an HTTP client without a search key")

    monkeypatch.setattr(cli, "make_http_client", no_network)
    result = runner.invoke(cli.app, ["discover"])
    assert result.exit_code == 0
    assert "BRAVE_SEARCH_API_KEY" in result.output
    assert "0 network calls" in result.output


def test_discover_dry_run_lists_queries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCRAPER_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setattr(cli, "make_http_client", lambda s: (_ for _ in ()).throw(AssertionError))
    result = runner.invoke(
        cli.app, ["discover", "--dry-run", "--provider", "ashby", "--cities", "pune"]
    )
    assert result.exit_code == 0, result.output
    assert "site:jobs.ashbyhq.com intern Pune" in result.output
    assert "site:jobs.ashbyhq.com intern India" in result.output
    assert "lever" not in result.output


def test_discover_cli_with_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCRAPER_DB_PATH", str(tmp_path / "db.sqlite"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-key")
    handler = ats_handler(discovery_boards(), [])
    monkeypatch.setattr(cli, "make_http_client", lambda s: make_client(handler, FakeTime()))
    monkeypatch.setattr(cli, "search_backend_from_settings",
                        lambda http, s: FakeBackend(hits("brave.json")))  # fmt: skip
    result = runner.invoke(cli.app, ["discover", "--cities", "hyderabad"])
    assert result.exit_code == 0, result.output
    assert "saved 2 startups" in result.output
    assert "1 leads" in result.output
    with Repository.open(tmp_path / "db.sqlite") as repo:
        assert repo.get_startup_by_domain("chaiops.example") is not None


async def test_discover_job_without_key_is_friendly(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await jobs.discover_job(
        Settings.from_env({}),
        http_factory=lambda s: (_ for _ in ()).throw(AssertionError("no network expected")),
    )
    assert not result.ok
    assert result.error == jobs.NO_SEARCH_KEY
