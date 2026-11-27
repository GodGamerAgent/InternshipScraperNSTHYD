"""Shared pytest fixtures. Everything here is offline."""

import asyncio
import random
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.core.config import PROJECT_ROOT
from src.core.http import HttpClient, InMemoryResponseCache
from src.enrichment.geo import GeoResolver
from src.storage import Repository

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG_DIR = PROJECT_ROOT / "config"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test tries to open a network connection."""

    def guard(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Network access is forbidden in tests — use fixtures.")

    monkeypatch.setattr(socket.socket, "connect", guard)


TEST_UA = "india-internship-scraper/0.1 (+https://example.invalid/tests)"


class FakeTime:
    """Deterministic clock + sleep: ``sleep(d)`` advances ``now`` instantly and records ``d``."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)  # still yield to the event loop like a real sleep


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


def make_client(
    handler: Callable[[httpx.Request], Any],
    fake_time: FakeTime,
    **kwargs: Any,
) -> HttpClient:
    """``HttpClient`` over ``httpx.MockTransport(handler)`` with fake time and a seeded RNG."""
    kwargs.setdefault("cache", InMemoryResponseCache())
    return HttpClient(
        user_agent=TEST_UA,
        transport=httpx.MockTransport(handler),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        rng=random.Random(42),
        **kwargs,
    )


@pytest.fixture(scope="session")
def geo() -> GeoResolver:
    """Resolver built from the real ``config/india_geo.yaml``."""
    return GeoResolver.from_config(CONFIG_DIR)


@pytest.fixture
def repo(tmp_path: Path):
    """Fresh file-backed repository in a temp dir (never touches ``data/``)."""
    with Repository.open(tmp_path / "test.db") as repository:
        yield repository
