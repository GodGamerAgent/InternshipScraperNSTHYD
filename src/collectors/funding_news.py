"""Inc42 + Entrackr funding-news collector (RSS via ``feedparser``).

Feeds come from ``sources.yaml: funding_feeds`` (Inc42 ``/startups/feed/``, Entrackr ``/rss``).
Both are plain RSS pages on news sites, so robots.txt is respected (both allow these paths).
Each single-company funding headline becomes a ``StartupLead`` with stage, amount, date and HQ
city; news items carry no company website, so leads usually have ``domain=None`` (ADR 004).
"""

import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

import feedparser

from src.collectors.config import FeedConfig
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.core.text import html_to_text
from src.enrichment.funding import parse_funding_headline
from src.enrichment.geo import GeoResolver
from src.models import StartupLead

logger = logging.getLogger(__name__)


def _published(entry: object) -> date | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=UTC).date()


class FundingNewsCollector(BaseCollector[FeedConfig, str, StartupLead]):
    """One target per configured feed; ``StartupLead.source`` is the feed name (inc42/entrackr)."""

    name = "funding_news"

    def __init__(self, http: HttpClient, *, feeds: list[FeedConfig], geo: GeoResolver) -> None:
        super().__init__(http)
        self.feeds = feeds
        self.geo = geo

    def targets(self) -> list[FeedConfig]:
        return list(self.feeds)

    def describe_target(self, target: FeedConfig) -> str:
        return f"{target.name}:{urlsplit(target.url).path}"

    async def fetch(self, target: FeedConfig) -> str:
        return await self.http.get_text(target.url)  # news site page: robots.txt respected

    def parse(self, raw: str, target: FeedConfig) -> Iterable[StartupLead]:
        feed = feedparser.parse(raw)
        if feed.bozo and not feed.entries:
            raise ValueError(f"not a parseable RSS/Atom feed: {feed.get('bozo_exception')!r}")
        leads: list[StartupLead] = []
        for entry in feed.entries:
            title = entry.get("title") or ""
            summary = html_to_text(entry.get("summary") or "")
            event = parse_funding_headline(title, summary)
            if event is None:
                continue
            lead = StartupLead(
                name=event.company,
                source=target.name,
                source_url=entry.get("link") or None,
                funding_stage=event.stage,
                funding_amount=event.amount,
                last_funded_at=_published(entry),
            )
            if event.city:
                location = self.geo.resolve(event.city)
                if location.is_resolved:
                    lead = lead.with_hq(location)
            leads.append(lead)
        logger.debug(
            "collector.feed_parsed",
            extra={"collector": self.name, "target": target.name, "entries": len(feed.entries),
                   "leads": len(leads)},
        )  # fmt: skip
        return leads
