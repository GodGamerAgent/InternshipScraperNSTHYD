"""Offline tests for ``src/core/http.py`` using ``httpx.MockTransport`` and fake time."""

import asyncio
import itertools
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from src.core.config import Settings
from src.core.http import (
    CachedResponse,
    FetchError,
    FileResponseCache,
    HttpClient,
    HttpPolicy,
    RobotsDisallowedError,
    is_from_cache,
    parse_retry_after,
)
from tests.conftest import TEST_UA, FakeTime, make_client

OPEN_ROBOTS = httpx.Response(200, text="User-agent: *\nAllow: /\n")


def ok(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return OPEN_ROBOTS
    return httpx.Response(200, json={"path": request.url.path})


# ---------------------------------------------------------------- identity
async def test_user_agent_sent_on_every_request(fake_time: FakeTime) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return ok(request)

    async with make_client(handler, fake_time) as http:
        await http.get("https://acme.in/jobs")
    assert seen == [TEST_UA, TEST_UA]  # robots.txt + page


# ---------------------------------------------------------------- rate limiting
async def test_same_host_requests_are_serialized_with_jittered_gap(fake_time: FakeTime) -> None:
    starts: list[float] = []
    in_flight = 0
    max_in_flight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        starts.append(fake_time.now)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return httpx.Response(200)

    async with make_client(handler, fake_time) as http:
        await asyncio.gather(
            *(http.get(f"https://acme.in/{i}", respect_robots=False) for i in range(5))
        )
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert starts[0] == 0  # first request to a host never waits
    assert max_in_flight == 1
    assert len(gaps) == 4
    assert all(1.5 <= g <= 2.5 for g in gaps), gaps
    assert len(set(gaps)) > 1  # jittered, not a fixed interval


async def test_different_hosts_do_not_wait_for_each_other(fake_time: FakeTime) -> None:
    starts: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        starts[request.url.host] = fake_time.now
        return httpx.Response(200)

    async with make_client(handler, fake_time) as http:
        await asyncio.gather(
            *(http.get(f"https://host{i}.in/", respect_robots=False) for i in range(4))
        )
    assert set(starts.values()) == {0.0}


async def test_global_concurrency_is_capped_at_five(fake_time: FakeTime) -> None:
    in_flight = 0
    max_in_flight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.005)  # real (tiny) wait so requests genuinely overlap
        in_flight -= 1
        return httpx.Response(200)

    async with make_client(handler, fake_time) as http:
        await asyncio.gather(
            *(http.get(f"https://host{i}.in/", respect_robots=False) for i in range(12))
        )
    assert max_in_flight == 5


# ---------------------------------------------------------------- retries
def scripted(*responses: httpx.Response | Exception):
    """Handler returning/raising the given responses in order (then repeating the last)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return handler, calls


async def test_retries_5xx_then_succeeds(fake_time: FakeTime) -> None:
    handler, calls = scripted(httpx.Response(503), httpx.Response(502), httpx.Response(200))
    async with make_client(handler, fake_time) as http:
        response = await http.get("https://acme.in/", respect_robots=False)
    assert response.status_code == 200
    assert len(calls) == 3
    assert len(fake_time.sleeps) == 2
    assert fake_time.sleeps[1] > fake_time.sleeps[0] >= 1.5  # exponential backoff, ≥ polite gap


async def test_retry_after_seconds_is_honoured(fake_time: FakeTime) -> None:
    handler, _ = scripted(httpx.Response(429, headers={"Retry-After": "17"}), httpx.Response(200))
    async with make_client(handler, fake_time) as http:
        await http.get("https://acme.in/", respect_robots=False)
    assert fake_time.sleeps == [17.0]


async def test_retry_after_is_capped(fake_time: FakeTime) -> None:
    handler, _ = scripted(
        httpx.Response(503, headers={"Retry-After": "86400"}), httpx.Response(200)
    )
    async with make_client(handler, fake_time) as http:
        await http.get("https://acme.in/", respect_robots=False)
    assert fake_time.sleeps == [120.0]


async def test_gives_up_after_three_retries(fake_time: FakeTime) -> None:
    handler, calls = scripted(httpx.Response(500))
    async with make_client(handler, fake_time) as http:
        with pytest.raises(FetchError, match="gave up after 4 attempts") as info:
            await http.get("https://acme.in/", respect_robots=False)
    assert info.value.status == 500
    assert len(calls) == 4


async def test_transport_errors_are_retried(fake_time: FakeTime) -> None:
    handler, calls = scripted(httpx.ConnectError("boom"), httpx.Response(200))
    async with make_client(handler, fake_time) as http:
        assert (await http.get("https://acme.in/", respect_robots=False)).status_code == 200
    assert len(calls) == 2


async def test_persistent_transport_error_raises_fetch_error(fake_time: FakeTime) -> None:
    handler, _ = scripted(httpx.ReadTimeout("slow"))
    async with make_client(handler, fake_time) as http:
        with pytest.raises(FetchError) as info:
            await http.get("https://acme.in/", respect_robots=False)
    assert info.value.status is None


async def test_4xx_is_not_retried(fake_time: FakeTime) -> None:
    handler, calls = scripted(httpx.Response(404))
    async with make_client(handler, fake_time) as http:
        assert (await http.get("https://acme.in/", respect_robots=False)).status_code == 404
        with pytest.raises(FetchError, match="unexpected HTTP status") as info:
            await http.get_json("https://acme.in/", respect_robots=False)
    assert info.value.status == 404
    assert len(calls) == 2


async def test_get_json_rejects_invalid_json(fake_time: FakeTime) -> None:
    handler, _ = scripted(httpx.Response(200, text="<html>not json</html>"))
    async with make_client(handler, fake_time) as http:
        with pytest.raises(FetchError, match="invalid JSON"):
            await http.get_json("https://acme.in/", respect_robots=False)


def test_parse_retry_after() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert parse_retry_after("30") == 30
    assert parse_retry_after("Thu, 26 Nov 2026 12:00:45 GMT", now=now) == 45
    assert parse_retry_after("Thu, 26 Nov 2026 11:00:00 GMT", now=now) == 0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


# ---------------------------------------------------------------- conditional requests
async def test_etag_revalidation_serves_cached_body(fake_time: FakeTime) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json={"jobs": [1, 2]}, headers={"ETag": '"v1"'})

    async with make_client(handler, fake_time) as http:
        first = await http.get("https://acme.in/jobs", respect_robots=False)
        second = await http.get("https://acme.in/jobs", respect_robots=False)
    assert seen == [None, '"v1"']
    assert not is_from_cache(first)
    assert is_from_cache(second)
    assert second.status_code == 200
    assert second.json() == {"jobs": [1, 2]}


async def test_last_modified_sends_if_modified_since(fake_time: FakeTime) -> None:
    stamp = "Wed, 25 Nov 2026 10:00:00 GMT"
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-modified-since"))
        return httpx.Response(200, text="feed", headers={"Last-Modified": stamp})

    async with make_client(handler, fake_time) as http:
        await http.get("https://acme.in/feed", respect_robots=False)
        await http.get("https://acme.in/feed", respect_robots=False)
        await http.get("https://acme.in/feed", respect_robots=False, use_cache=False)
    assert seen == [None, stamp, None]


async def test_responses_without_validators_are_not_cached(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "if-none-match" not in request.headers
        return httpx.Response(200, text="no validators")

    async with make_client(handler, fake_time) as http:
        await http.get("https://acme.in/x", respect_robots=False)
        await http.get("https://acme.in/x", respect_robots=False)


def test_file_cache_roundtrip_and_corruption(tmp_path: Path) -> None:
    cache = FileResponseCache(tmp_path / "cache")
    entry = CachedResponse("https://acme.in/a", b"body", {"etag": '"x"'}, stored_at=1.0)
    assert cache.get(entry.url) is None
    cache.set(entry.url, entry)
    assert cache.get(entry.url) == entry
    for meta in (tmp_path / "cache").glob("*.json"):
        meta.write_text("{not json")
    assert cache.get(entry.url) is None


async def test_file_cache_survives_between_clients(tmp_path: Path, fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v7"':
            return httpx.Response(304)
        return httpx.Response(200, text="board v7", headers={"ETag": '"v7"'})

    for expected_cached in (False, True):  # two separate "CLI runs"
        cache = FileResponseCache(tmp_path / "http_cache")
        async with make_client(handler, fake_time, cache=cache) as http:
            response = await http.get("https://acme.in/b", respect_robots=False)
        assert response.text == "board v7"
        assert is_from_cache(response) is expected_cached


# ---------------------------------------------------------------- robots integration
async def test_robots_disallow_blocks_without_requesting_page(fake_time: FakeTime) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /team\n")
        return httpx.Response(200)

    async with make_client(handler, fake_time) as http:
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://acme.in/team/founders")
        await http.get("https://acme.in/careers")
        await http.get("https://acme.in/team/x", respect_robots=False)  # documented-API opt-out
    assert paths == ["/robots.txt", "/careers", "/team/x"]  # robots fetched once


async def test_robots_unreachable_blocks_everything(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.path == "/robots.txt" else 200)

    async with make_client(handler, fake_time) as http:
        with pytest.raises(RobotsDisallowedError):
            await http.get("https://acme.in/careers")


async def test_robots_404_allows_everything(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.url.path == "/robots.txt" else 200)

    async with make_client(handler, fake_time) as http:
        assert (await http.get("https://acme.in/careers")).status_code == 200


async def test_crawl_delay_raises_gap(fake_time: FakeTime) -> None:
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nCrawl-delay: 10\n")
        starts.append(fake_time.now)
        return httpx.Response(200)

    async with make_client(handler, fake_time) as http:
        await http.get("https://acme.in/1")
        await http.get("https://acme.in/2")
    assert starts[1] - starts[0] >= 10


# ---------------------------------------------------------------- construction
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrency": 6},
        {"max_concurrency": 0},
        {"max_retries": 4},
        {"min_delay_s": 3.0, "max_delay_s": 2.0},
        {"timeout_s": 0},
    ],
)
def test_policy_enforces_compliance_caps(kwargs: dict) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - message varies per field
        HttpPolicy(**kwargs)


async def test_from_settings_uses_env_user_agent_and_file_cache(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {"SCRAPER_USER_AGENT": "custom-bot/1.0", "SCRAPER_HTTP_CACHE_DIR": str(tmp_path / "c")}
    )
    async with HttpClient.from_settings(settings) as http:
        assert http.user_agent == "custom-bot/1.0"
        assert isinstance(http.cache, FileResponseCache)
        assert http.cache.directory == tmp_path / "c"


def test_invalid_user_agent_rejected() -> None:
    with pytest.raises(ValueError, match="product token"):
        HttpClient(user_agent="")


async def test_post_json_sends_body_and_retries(fake_time: FakeTime) -> None:
    bodies: list[bytes] = []
    handler, calls = scripted(httpx.Response(503), httpx.Response(200, json={"ok": True}))

    def recording(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return handler(request)

    async with make_client(recording, fake_time) as http:
        reply = await http.post_json("https://api.example.com/v1/x", {"url": "https://a.in"})
    assert reply == {"ok": True}
    assert len(calls) == 2
    assert bodies[0] == b'{"url":"https://a.in"}'


async def test_post_json_raises_on_error_status(fake_time: FakeTime) -> None:
    handler, _ = scripted(httpx.Response(401, json={"error": "bad key"}))
    async with make_client(handler, fake_time) as http:
        with pytest.raises(FetchError) as info:
            await http.post_json("https://api.example.com/v1/x", {})
    assert info.value.status == 401
