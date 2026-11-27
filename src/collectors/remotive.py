"""Remotive remote internships open to India, from its public RSS feed (ADR 010).

Source: ``https://remotive.com/remote-jobs/feed`` (robots.txt allows it and is respected). The
JSON API (``/api/remote-jobs``) is **not** used, because remotive.com's robots.txt disallows
``/api/*``. ``/v2/feed`` and ``/remote-jobs/digest/feed`` are disallowed too.

Feed shape, verified 2026-11-26 (17 latest items): ``title``, ``company``, ``location`` (free
text: "Worldwide", "USA", "LATAM, Europe, USA, Canada, APAC", "India"…), ``type``
(``full_time`` / ``contract`` / ``part_time`` / ``freelance`` / ``internship``), ``link``,
``pubDate``, ``category``, ``description`` (HTML). ``feedparser`` exposes the custom elements as
entry keys.

The feed only carries the latest items, so a posting that scrolls out is closed on the next
successful read (``close_scope = "*"``), like an expired listing.
"""

import logging
from calendar import timegm
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import feedparser
from pydantic import ValidationError

from src.collectors.config import RemoteFeedsConfig, RemotiveConfig
from src.collectors.remote_base import IndiaOpenness, remote_opportunity
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.core.text import html_to_text
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import CollectedOpportunity

logger = logging.getLogger(__name__)


class RemotiveCollector(BaseCollector[RemotiveConfig, bytes, CollectedOpportunity]):
    """Single target: the configured RSS feed."""

    name = "remotive"

    def __init__(
        self,
        http: HttpClient,
        *,
        config: RemoteFeedsConfig,
        geo: GeoResolver,
        matcher: InternshipMatcher,
    ) -> None:
        super().__init__(http)
        self.config = config
        self.openness = IndiaOpenness(config, geo)
        self.matcher = matcher

    def targets(self) -> list[RemotiveConfig]:
        return [self.config.remotive] if self.config.remotive is not None else []

    def describe_target(self, target: RemotiveConfig) -> str:
        return "feed"

    def close_scope(self, target: RemotiveConfig) -> str | None:
        return "*"

    async def fetch(self, target: RemotiveConfig) -> bytes:
        response = await self.http.get(target.feed_url)  # RSS page: robots.txt respected
        return response.content

    def parse(self, raw: bytes, target: RemotiveConfig) -> Iterable[CollectedOpportunity]:
        feed = feedparser.parse(raw)
        if feed.bozo and not feed.entries:
            raise ValueError(f"unreadable RSS feed: {feed.get('bozo_exception')}")
        items: dict[str, CollectedOpportunity] = {}
        for entry in feed.entries:
            try:
                item = self._item(entry)
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                logger.warning(
                    "collector.posting_skipped",
                    extra={"collector": self.name, "target": "feed",
                           "error_type": type(exc).__name__, "error": str(exc)[:300]},
                )  # fmt: skip
                continue
            if item is not None:
                items.setdefault(item.opportunity.url, item)
        return list(items.values())

    def _item(self, entry: Any) -> CollectedOpportunity | None:
        title = " ".join(str(entry["title"]).split())
        if not self.matcher.is_internship(title, employment_type=entry.get("type")):
            return None
        location = self.openness.free_text(str(entry.get("location") or ""))
        if location is None:
            return None
        published = entry.get("published_parsed")
        company = str(entry.get("company") or "").strip()
        body = html_to_text(entry.get("summary") or entry.get("description"))
        return remote_opportunity(
            source=self.name,
            title=title,
            url=str(entry["link"]),
            description=f"{company}\n{body}".strip() if company else body,
            location=location,
            posted_at=datetime.fromtimestamp(timegm(published), UTC) if published else None,
        )
