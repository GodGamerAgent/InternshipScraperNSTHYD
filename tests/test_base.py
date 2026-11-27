"""Offline tests for ``src/core/base.py`` — the collector contract and fault isolation."""

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any

import httpx
import pytest

from src.core.base import BaseCollector, CollectorResult, run_all
from src.core.http import HttpClient
from src.enrichment.geo import GeoResolver
from src.models import Opportunity, WorkMode
from tests.conftest import FIXTURES, FakeTime, make_client

BOARDS = {
    "acme": FIXTURES / "collectors" / "demo_board_acme.json",
    "broken": FIXTURES / "collectors" / "demo_board_broken.json",
}


def board_handler(request: httpx.Request) -> httpx.Response:
    slug = request.url.path.strip("/").split("/")[-1]
    if slug in BOARDS:
        return httpx.Response(200, content=BOARDS[slug].read_bytes())
    return httpx.Response(404, json={"error": "board not found"})


class DemoBoardCollector(BaseCollector[str, dict, Opportunity]):
    """Minimal realistic collector: one ATS-style JSON board per company slug."""

    name = "demo_board"

    def __init__(self, http: HttpClient, geo: GeoResolver, slugs: list[str]) -> None:
        super().__init__(http)
        self.geo = geo
        self.slugs = slugs

    def targets(self) -> list[str]:
        return self.slugs

    async def fetch(self, target: str) -> dict:
        # Documented public JSON API -> robots.txt opt-out (see ADR 002).
        return await self.http.get_json(
            f"https://ats.example.com/boards/{target}", respect_robots=False
        )

    def parse(self, raw: dict, target: str) -> Iterable[Opportunity]:
        for job in raw["jobs"]:
            location = self.geo.resolve(job["location"])
            yield Opportunity(source=self.name, title=job["title"], url=job["url"]).with_location(
                location
            )


@pytest.fixture
def http(fake_time: FakeTime) -> HttpClient:
    return make_client(board_handler, fake_time)


# ---------------------------------------------------------------- pure parse from fixture
def test_parse_is_pure_and_fixture_testable(geo: GeoResolver, http: HttpClient) -> None:
    raw = json.loads(BOARDS["acme"].read_text(encoding="utf-8"))
    items = list(DemoBoardCollector(http, geo, []).parse(raw, "acme"))
    assert [(o.city_canonical, o.work_mode) for o in items] == [
        ("hyderabad", WorkMode.ONSITE),
        ("remote_india", WorkMode.REMOTE_INDIA),
        ("bengaluru", WorkMode.HYBRID),
    ]
    assert {o.source for o in items} == {"demo_board"}


# ---------------------------------------------------------------- fault isolation
async def test_one_bad_target_never_stops_the_others(geo: GeoResolver, http: HttpClient) -> None:
    collector = DemoBoardCollector(http, geo, ["acme", "broken", "missing"])
    async with http:
        result = await collector.run()

    assert len(result.items) == 3  # all of acme's jobs survived
    assert result.targets_total == 3
    assert result.targets_failed == 2
    assert not result.ok
    issues = {i.target: i for i in result.issues}
    assert issues["broken"].stage == "parse"
    assert issues["broken"].error_type == "ValidationError"
    assert issues["missing"].stage == "fetch"
    assert issues["missing"].error_type == "FetchError"
    assert issues["missing"].status == 404
    assert result.duration_s >= 0
    assert result.started_at.tzinfo is not None


async def test_target_timeout_is_isolated(fake_time: FakeTime) -> None:
    class SlowCollector(BaseCollector[str, str, str]):
        name = "slow_source"
        target_timeout_s = 0.01

        def targets(self) -> list[str]:
            return ["fast", "hangs"]

        async def fetch(self, target: str) -> str:
            if target == "hangs":
                await asyncio.Event().wait()  # never completes
            return target

        def parse(self, raw: str, target: str) -> Iterable[str]:
            return [raw.upper()]

    async with make_client(board_handler, fake_time) as http:
        result = await SlowCollector(http).run()
    assert result.items == ["FAST"]
    assert [(i.target, i.stage, i.error_type) for i in result.issues] == [
        ("hangs", "fetch", "TimeoutError")
    ]


async def test_failing_targets_hook_is_captured(fake_time: FakeTime) -> None:
    class BadConfigCollector(BaseCollector[str, str, str]):
        name = "bad_config"

        def targets(self) -> list[str]:
            raise KeyError("ats.lever.companies")

        async def fetch(self, target: str) -> str:  # pragma: no cover
            return target

        def parse(self, raw: str, target: str) -> Iterable[str]:  # pragma: no cover
            return []

    async with make_client(board_handler, fake_time) as http:
        result = await BadConfigCollector(http).run()
    assert result.items == []
    assert [(i.stage, i.error_type) for i in result.issues] == [("targets", "KeyError")]


async def test_run_all_isolates_crashing_collectors(geo: GeoResolver, http: HttpClient) -> None:
    class CrashingCollector(DemoBoardCollector):
        name = "crashing"

        async def run(self) -> CollectorResult[Opportunity]:  # simulates a bug in an override
            raise RuntimeError("bug outside the per-target boundary")

    async with http:
        results = await run_all(
            [CrashingCollector(http, geo, ["acme"]), DemoBoardCollector(http, geo, ["acme"])]
        )
    crashed, healthy = results
    assert crashed.collector == "crashing"
    assert [(i.stage, i.error_type) for i in crashed.issues] == [("run", "RuntimeError")]
    assert healthy.ok
    assert len(healthy.items) == 3


# ---------------------------------------------------------------- contract enforcement
def test_name_must_be_a_slug() -> None:
    with pytest.raises(TypeError, match="snake_case"):

        class BadName(BaseCollector[None, None, None]):
            name = "Bad Name"

            async def fetch(self, target: None) -> None: ...

            def parse(self, raw: None, target: None) -> Iterable[None]:
                return []


def test_name_is_required(http: HttpClient) -> None:
    class Nameless(BaseCollector[None, None, None]):
        async def fetch(self, target: None) -> None: ...

        def parse(self, raw: None, target: None) -> Iterable[None]:
            return []

    with pytest.raises(TypeError, match="must define"):
        Nameless(http)


def test_fetch_and_parse_are_abstract(http: HttpClient) -> None:
    class Incomplete(BaseCollector[None, None, None]):
        name = "incomplete"

    with pytest.raises(TypeError, match="abstract"):
        Incomplete(http)  # type: ignore[abstract]


async def test_default_single_target(fake_time: FakeTime) -> None:
    class FeedCollector(BaseCollector[None, str, str]):
        name = "feed"

        async def fetch(self, target: None) -> str:
            assert target is None
            return "item"

        def parse(self, raw: str, target: None) -> Iterable[str]:
            return [raw]

    async with make_client(board_handler, fake_time) as http:
        result = await FeedCollector(http).run()
    assert result.items == ["item"]
    assert result.targets_total == 1
    assert FeedCollector(http).describe_target(None) == "default"


# ---------------------------------------------------------------- structured logging
async def test_structured_log_events(
    geo: GeoResolver, http: HttpClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with http:
        await DemoBoardCollector(http, geo, ["acme", "missing"]).run()
    events: dict[str, Any] = {r.getMessage(): r for r in caplog.records}
    assert {"collector.start", "collector.fetch_failed", "collector.finished"} <= set(events)
    finished = events["collector.finished"]
    assert finished.collector == "demo_board"
    assert finished.items == 3
    assert finished.targets_failed == 1
    assert events["collector.fetch_failed"].target == "missing"
