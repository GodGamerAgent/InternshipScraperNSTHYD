"""Internshala public internship listings by city (ADR 011).

Pages (``sources.yaml: student_portals.internshala``): ``/internships/internship-in-{city}/``
and ``/internships/work-from-home-internships/``, plus ``page-N/`` for later pages. robots.txt
allows these and is respected. It disallows query strings, ``/internship/search/`` and
``/internship/details/``, so detail pages are never fetched; the card's link is stored only as the
opportunity URL.

Card markup, verified 2026-11-26 (50 per page, server-rendered):

* ``div.individual_internship[internshipid][data-href][employment_type="internship"]``
* ``a.job-title-href`` (title) and ``p.company-name``
* ``.locations a`` / ``.locations span`` (one or more cities, or "Work from home")
* ``span.stipend`` ("₹ 15,000 - 25,000 /month", "Unpaid")
* the ``row-1-item`` with ``i.ic-16-calendar`` holds the duration ("3 Months")
* ``.about_job .text`` (summary)

**Terms of Service:** Internshala's Terms prohibit automated access. The project owner chose to
collect public listings anyway (ADR 011). The collector stays polite: robots.txt, the per-host
delay, bounded pages, and no login, CAPTCHA or firewall bypass.
"""

import logging
from collections.abc import Collection, Iterable
from typing import Any

from bs4 import BeautifulSoup, Tag
from pydantic import ValidationError

from src.collectors.config import InternshalaConfig
from src.collectors.portal_base import india_portal_location
from src.collectors.remote_base import remote_opportunity
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import CollectedOpportunity

logger = logging.getLogger(__name__)

CARD_SELECTOR = "div.individual_internship[internshipid]"


def cards(html: str) -> list[Tag]:
    """Internship cards on one listing page."""
    return BeautifulSoup(html, "lxml").select(CARD_SELECTOR)


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def card_fields(card: Tag) -> dict[str, str]:
    """Title, href, company, locations, stipend, duration, summary and kind of one card."""
    duration = ""
    for item in card.select(".row-1-item"):
        if item.select_one("i.ic-16-calendar"):
            duration = _text(item)
    places = [_text(a) for a in card.select(".locations a")] or [
        _text(s) for s in card.select(".locations span")
    ]
    return {
        "id": str(card.get("internshipid") or ""),
        "kind": str(card.get("employment_type") or "internship"),
        "title": _text(card.select_one("a.job-title-href")),
        "href": str(card.get("data-href") or ""),
        "company": _text(card.select_one(".company-name")),
        "locations": " / ".join(dict.fromkeys(p for p in places if p)),
        "stipend": _text(card.select_one(".stipend")),
        "duration": duration,
        "summary": _text(card.select_one(".about_job .text")),
    }


class InternshalaCollector(BaseCollector[InternshalaConfig, list[Any], CollectedOpportunity]):
    """Single target: every selected city/remote listing (``max_pages`` each)."""

    name = "internshala"

    def __init__(
        self,
        http: HttpClient,
        *,
        config: InternshalaConfig | None,
        geo: GeoResolver,
        matcher: InternshipMatcher,
        cities: Collection[str] | None = None,
    ) -> None:
        super().__init__(http)
        self.config = config
        self.geo = geo
        self.matcher = matcher
        self.cities = set(cities) if cities is not None else None

    def selected(self, config: InternshalaConfig) -> dict[str, str]:
        """Targets narrowed by ``--cities``; ``remote`` is always kept."""
        if self.cities is None:
            return dict(config.targets)
        return {k: v for k, v in config.targets.items() if k == "remote" or k in self.cities}

    def targets(self) -> list[InternshalaConfig]:
        return [self.config] if self.config is not None and self.selected(self.config) else []

    def describe_target(self, target: InternshalaConfig) -> str:
        return ",".join(self.selected(target))

    def close_scope(self, target: InternshalaConfig) -> str | None:
        return "*"

    async def fetch(self, target: InternshalaConfig) -> list[Any]:
        pages: list[Any] = []
        for key, path in self.selected(target).items():
            for page in range(1, target.max_pages + 1):
                url = f"{target.base_url}{path}" + (f"page-{page}/" if page > 1 else "")
                html = await self.http.get_text(url)  # HTML listing: robots.txt respected
                pages.append([key, html])
                if not cards(html):
                    break
        return pages

    def parse(self, raw: list[Any], target: InternshalaConfig) -> Iterable[CollectedOpportunity]:
        items: dict[str, CollectedOpportunity] = {}
        for key, html in raw:
            for card in cards(html):
                fields = card_fields(card)
                try:
                    item = self._item(fields, target, remote=key == "remote")
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    logger.warning(
                        "collector.posting_skipped",
                        extra={"collector": self.name, "target": key,
                               "error_type": type(exc).__name__, "error": str(exc)[:300]},
                    )  # fmt: skip
                    continue
                if item is not None:
                    items.setdefault(fields["id"], item)
        return list(items.values())

    def _item(
        self, fields: dict[str, str], target: InternshalaConfig, *, remote: bool
    ) -> CollectedOpportunity | None:
        if not fields["id"] or not fields["title"] or not fields["href"].startswith("/"):
            raise ValueError(f"incomplete card {fields['id']!r}")
        if fields["kind"] != "internship" or not self.matcher.is_internship(
            fields["title"], employment_type="internship"
        ):
            return None
        location = india_portal_location(
            self.geo, fields["locations"], workplace="remote" if remote else None
        )
        if location is None:
            return None
        details = [
            fields["company"],
            f"Stipend: {fields['stipend']}" if fields["stipend"] else "",
            f"Duration: {fields['duration']}" if fields["duration"] else "",
            fields["summary"],
        ]
        return remote_opportunity(
            source=self.name,
            title=fields["title"],
            url=f"{target.base_url}{fields['href']}",
            description="\n".join(d for d in details if d),
            location=location,
            posted_at=None,  # cards only say "1 week ago"
        )
