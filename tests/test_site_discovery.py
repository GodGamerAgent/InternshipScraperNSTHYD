"""Offline tests for site fetching, ATS/cold-pitch discovery and GitHub signals."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from src.collectors.config import SourcesConfig
from src.core.http import FetchError
from src.enrichment.ats_discovery import analyze_site, find_ats, find_github_orgs
from src.enrichment.github_signals import compute_signals, fetch_github_signals
from src.enrichment.site import SitePage, fetch_site_pages
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
DOMAIN = "examplestack.example"


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


def html(name: str) -> str:
    return (FIXTURES / "site" / name).read_text(encoding="utf-8")


def page(path: str, name: str, final_url: str | None = None) -> SitePage:
    url = f"https://{DOMAIN}{path}"
    return SitePage(path, url, final_url or url, 200, html(name))


# ---------------------------------------------------------------- pure discovery
def test_find_ats_providers_and_slugs(sources: SourcesConfig) -> None:
    found = find_ats(html("careers.html"), enabled_providers=sources.ats_embed_hosts)
    assert found == [
        ("greenhouse", "examplestack"),
        ("lever", "examplestack-in"),
        ("keka", "examplestack"),
    ]
    assert find_ats("https://jobs.ashbyhq.com/acme/123", enabled_providers=["ashby"]) == [
        ("ashby", "acme")
    ]
    assert find_ats("https://apply.workable.com/j/ABC123", enabled_providers=["workable"]) == []
    assert find_ats("https://jobs.lever.co/acme", enabled_providers=["greenhouse"]) == []


def test_find_github_orgs_skips_reserved_paths() -> None:
    assert find_github_orgs(html("home.html")) == ["examplestack-oss"]


def test_analyze_site(sources: SourcesConfig) -> None:
    pages = [
        page("/", "home.html"),
        page("/careers", "careers.html"),
        page("/jobs", "careers.html", final_url="https://boards.greenhouse.io/examplestack-two"),
        SitePage("/team", f"https://{DOMAIN}/team", f"https://{DOMAIN}/team", None,
                 error="robots_disallowed"),
        SitePage("/join", f"https://{DOMAIN}/join", f"https://{DOMAIN}/join", 200,
                 html("spa_shell.html"), js_only=True),
    ]  # fmt: skip
    found = analyze_site(
        pages,
        cold_pitch_phrases=sources.cold_pitch_phrases,
        ats_embed_hosts=sources.ats_embed_hosts,
    )
    assert [(m.provider, m.slug) for m in found.ats] == [
        ("greenhouse", "examplestack"),
        ("lever", "examplestack-in"),
        ("keka", "examplestack"),
        ("greenhouse", "examplestack-two"),  # from the /jobs redirect target
    ]
    assert found.primary_tier1 is not None
    assert (found.primary_tier1.provider, found.primary_tier1.slug) == (
        "greenhouse",
        "examplestack",
    )
    assert not next(m for m in found.ats if m.provider == "keka").tier1
    assert found.open_to_cold_pitch is True
    assert {"drop your resume", "don't see a role"} <= set(found.cold_pitch_evidence)
    assert found.github_orgs == ("examplestack-oss",)
    assert found.blocked_pages == ("/team",)
    assert found.js_only_pages == ("/join",)
    assert found.pages_ok == 4


def test_no_cold_pitch_without_phrases(sources: SourcesConfig) -> None:
    found = analyze_site(
        [page("/", "home.html")],
        cold_pitch_phrases=sources.cold_pitch_phrases,
        ats_embed_hosts=sources.ats_embed_hosts,
    )
    assert found.open_to_cold_pitch is False
    assert found.ats == ()


# ---------------------------------------------------------------- fetching
class FakeRenderer:
    async def scrape_html(self, url: str) -> str:
        return "<html><body><p>Rendered careers: drop your resume</p></body></html>"


async def test_fetch_site_pages_respects_robots_and_follows_redirects(
    sources: SourcesConfig, fake_time: FakeTime
) -> None:
    seen: list[str] = []
    bodies = {"/": "home.html", "/careers": "careers.html", "/join": "spa_shell.html"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /team\n")
        if request.url.host == DOMAIN and request.url.path == "/jobs":
            return httpx.Response(
                302, headers={"Location": "https://boards.greenhouse.io/examplestack"}
            )
        if request.url.host == "boards.greenhouse.io":
            return httpx.Response(
                200, text="<html><body>" + "<p>Open roles</p>" * 50 + "</body></html>"
            )
        if request.url.path in bodies:
            return httpx.Response(200, text=html(bodies[request.url.path]))
        return httpx.Response(404)

    async with make_client(handler, fake_time) as http:
        pages = await fetch_site_pages(
            http, DOMAIN, sources.site.careers_paths, renderer=FakeRenderer()
        )
    by_path = {p.path: p for p in pages}
    assert list(by_path) == sources.site.careers_paths
    assert by_path["/team"].error == "robots_disallowed"
    assert f"{DOMAIN}/team" not in seen  # never requested
    assert by_path["/jobs"].final_url == "https://boards.greenhouse.io/examplestack"
    assert by_path["/about"].status == 404
    assert by_path["/join"].js_only
    assert by_path["/join"].rendered
    assert "drop your resume" in by_path["/join"].html


async def test_fetch_site_pages_caps_page_count(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200, text="<p>x</p>")

    async with make_client(handler, fake_time) as http:
        pages = await fetch_site_pages(http, DOMAIN, ["/a", "/b", "/a", "/c"], max_pages=2)
    assert [p.path for p in pages] == ["/a", "/b"]


# ---------------------------------------------------------------- GitHub signals
def gh(name: str):
    return json.loads((FIXTURES / "github" / name).read_text(encoding="utf-8"))


def test_compute_signals_ignores_forks_and_archived() -> None:
    signals = compute_signals(gh("org.json"), gh("repos.json"), now=NOW)
    assert signals.org == "examplestack-oss"
    assert signals.active_repos == 4  # cli, sdk-python, web, docs (fork/archived/legacy excluded)
    assert signals.recently_pushed is True  # cli pushed yesterday
    assert signals.last_pushed_at == datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    assert signals.languages[:2] == ("Go", "Python")
    assert signals.org_email == "oss@examplestack.example"
    assert signals.is_active(min_active_repos=3)


def test_stale_org_is_not_active() -> None:
    stale = [{"name": "old", "fork": False, "archived": False, "pushed_at": "2025-06-01T00:00:00Z"}]
    signals = compute_signals({"login": "sleepy"}, stale, now=NOW)
    assert signals.active_repos == 0
    assert not signals.recently_pushed
    assert not signals.is_active()


async def test_fetch_github_signals_uses_token_and_skips_robots(fake_time: FakeTime) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/repos"):
            return httpx.Response(200, json=gh("repos.json"))
        return httpx.Response(200, json=gh("org.json"))

    async with make_client(handler, fake_time) as http:
        signals = await fetch_github_signals(http, "examplestack-oss", token="ghp_test", now=NOW)
    assert signals is not None
    assert signals.active_repos == 4
    assert [r.url.path for r in requests] == [
        "/orgs/examplestack-oss",
        "/orgs/examplestack-oss/repos",
    ]
    assert all(r.headers["authorization"] == "Bearer ghp_test" for r in requests)
    assert requests[1].url.params["sort"] == "pushed"


async def test_fetch_github_signals_404_and_errors(fake_time: FakeTime) -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with make_client(missing, fake_time) as http:
        assert await fetch_github_signals(http, "nope") is None

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "API rate limit exceeded"})

    async with make_client(rate_limited, fake_time) as http:
        with pytest.raises(FetchError) as info:
            await fetch_github_signals(http, "examplestack-oss")
    assert info.value.status == 403
