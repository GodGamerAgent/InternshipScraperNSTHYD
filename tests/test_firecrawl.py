"""Offline tests for the optional Firecrawl fallback and the JS-only heuristic."""

import json

import httpx
import pytest

from src.core.config import Settings
from src.core.firecrawl_client import FirecrawlClient
from src.core.http import FetchError, HttpClient, RobotsDisallowedError
from src.core.text import looks_js_only
from tests.conftest import FakeTime, make_client

SPA_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'


def test_looks_js_only() -> None:
    assert looks_js_only(SPA_SHELL)
    assert looks_js_only("")
    content = (
        "<html><body><div id='root'>" + "<p>Real content here.</p>" * 40 + "</div></body></html>"
    )
    assert not looks_js_only(content)
    assert not looks_js_only("<html><body><p>Short but static page.</p></body></html>")


def test_disabled_without_api_key() -> None:
    http = HttpClient(user_agent="india-internship-scraper/0.1")
    assert FirecrawlClient.from_settings(http, Settings.from_env({})) is None
    enabled = FirecrawlClient.from_settings(http, Settings.from_env({"FIRECRAWL_API_KEY": "fc-x"}))
    assert isinstance(enabled, FirecrawlClient)
    assert "fc-x" not in repr(enabled)
    with pytest.raises(ValueError, match="API key"):
        FirecrawlClient(http, "  ")


def firecrawl_handler(
    seen: list[httpx.Request], *, robots: str = "User-agent: *\nAllow: /\n", reply=None
):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if request.url.host == "api.firecrawl.dev":
            return httpx.Response(
                200, json=reply or {"success": True, "data": {"html": "<p>rendered</p>"}}
            )
        return httpx.Response(404)

    return handler


async def test_scrape_html_checks_target_robots_then_calls_api(fake_time: FakeTime) -> None:
    seen: list[httpx.Request] = []
    async with make_client(firecrawl_handler(seen), fake_time) as http:
        html = await FirecrawlClient(http, "fc-secret").scrape_html(
            "https://directory.example/list"
        )
    assert html == "<p>rendered</p>"
    robots, api = seen
    assert str(robots.url) == "https://directory.example/robots.txt"
    assert api.url.path == "/v1/scrape"
    assert api.headers["authorization"] == "Bearer fc-secret"
    assert json.loads(api.content)["url"] == "https://directory.example/list"


async def test_scrape_html_refuses_robots_disallowed_targets(fake_time: FakeTime) -> None:
    seen: list[httpx.Request] = []
    handler = firecrawl_handler(seen, robots="User-agent: *\nDisallow: /\n")
    async with make_client(handler, fake_time) as http:
        with pytest.raises(RobotsDisallowedError):
            await FirecrawlClient(http, "fc-secret").scrape_html("https://directory.example/list")
    assert all(r.url.host != "api.firecrawl.dev" for r in seen)  # never delegated


@pytest.mark.parametrize(
    "reply",
    [{"success": False, "error": "quota"}, {"success": True, "data": {"html": "  "}}, ["bad"]],
)
async def test_scrape_html_errors(fake_time: FakeTime, reply) -> None:
    async with make_client(firecrawl_handler([], reply=reply), fake_time) as http:
        with pytest.raises(FetchError, match="firecrawl"):
            await FirecrawlClient(http, "fc-secret").scrape_html("https://directory.example/list")
