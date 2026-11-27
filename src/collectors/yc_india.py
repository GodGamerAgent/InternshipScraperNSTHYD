"""Y Combinator companies headquartered in India -> ``StartupLead``s (ADR 008).

Source: YC's public, server-rendered directory pages (``sources.yaml: yc_india``):

* ``https://www.ycombinator.com/companies/location/india?page=N`` — every India-HQ company,
* ``https://www.ycombinator.com/companies/location/india/hiring?page=N`` — the subset YC lists as
  currently hiring (there is no per-company hiring flag, so membership *is* the indicator).

Each page embeds its data as JSON in a ``data-page`` attribute: ``props.companies[]`` (``name``,
``slug``, ``website``, ``batch_name``, ``city``, ``country``, ``team_size``, ``github_url``,
``tags``, ``ycdc_status``) plus ``props.hasMore`` / ``props.totalPages`` (verified 2026-11-26:
177 companies on 4 pages, 52 hiring on 2). Both paths are allowed by ycombinator.com's
robots.txt and fetched with it respected; ``api.ycombinator.com`` is robots-disallowed and is
never used, nor are third-party mirrors.

Leads carry the website domain, so the pipeline promotes them to ``Startup`` rows and
``scraper enrich`` then discovers their ATS boards and public contacts. Only YC-listed facts are
stored (no founder data: the listing has none).
"""

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from src.collectors.config import YcDirectoryConfig
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.core.urls import canonical_domain, host_in
from src.enrichment.geo import GeoResolver
from src.models import StartupLead

logger = logging.getLogger(__name__)

COMPANY_URL = "https://www.ycombinator.com/companies/{slug}"
MAX_TAGS = 4


def page_props(html: str) -> Mapping[str, Any]:
    """``props`` of a directory page's ``data-page`` JSON. ``ValueError`` if absent/malformed."""
    node = BeautifulSoup(html, "lxml").find(attrs={"data-page": True})
    if node is None:
        raise ValueError("no data-page attribute: YC directory layout changed")
    try:
        props = json.loads(str(node["data-page"])).get("props")
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ValueError(f"malformed data-page JSON: {exc}") from exc
    if not isinstance(props, Mapping) or not isinstance(props.get("companies"), list):
        raise ValueError("data-page JSON has no props.companies list")
    return props


def _github_org(url: Any) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    parts = urlsplit(url.strip())
    if parts.hostname not in {"github.com", "www.github.com"}:
        return None
    segments = [s for s in parts.path.split("/") if s]
    return segments[0] if segments else None


class YcIndiaCollector(BaseCollector[YcDirectoryConfig, dict[str, Any], StartupLead]):
    """One target: the configured India listing (+ hiring listing), paginated in ``fetch``."""

    name = "yc_india"

    def __init__(
        self,
        http: HttpClient,
        *,
        config: YcDirectoryConfig | None,
        geo: GeoResolver,
        non_company_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(http)
        self.config = config
        self.geo = geo
        self.non_company_hosts = list(non_company_hosts or [])

    def targets(self) -> list[YcDirectoryConfig]:
        return [self.config] if self.config is not None else []

    def describe_target(self, target: YcDirectoryConfig) -> str:
        return "india"

    async def _pages(self, url: str, max_pages: int) -> list[Mapping[str, Any]]:
        pages: list[Mapping[str, Any]] = []
        for page in range(1, max_pages + 1):
            # Server-rendered HTML directory: robots.txt is respected (default).
            props = page_props(await self.http.get_text(url, params={"page": page}))
            pages.append(props)
            if not props.get("hasMore") or not props.get("companies"):
                break
        else:
            logger.warning("yc_india.max_pages_reached", extra={"url": url, "pages": max_pages})
        return pages

    async def fetch(self, target: YcDirectoryConfig) -> dict[str, Any]:
        hiring = await self._pages(target.hiring_url, target.max_pages) if target.hiring_url else []
        listing = await self._pages(target.listing_url, target.max_pages)
        return {"listing": listing, "hiring": hiring, "hiring_known": bool(target.hiring_url)}

    def parse(self, raw: dict[str, Any], target: YcDirectoryConfig) -> Iterable[StartupLead]:
        hiring_known = bool(raw.get("hiring_known"))
        hiring = [c for page in raw.get("hiring", []) for c in page.get("companies", [])]
        hiring_slugs = {c.get("slug") for c in hiring if isinstance(c, Mapping)}
        companies: dict[str, Mapping[str, Any]] = {}
        for company in [c for page in raw.get("listing", []) for c in page["companies"]] + hiring:
            if isinstance(company, Mapping) and isinstance(company.get("slug"), str):
                companies.setdefault(company["slug"], company)

        statuses = {s.lower() for s in target.statuses}
        leads: list[StartupLead] = []
        for slug, company in companies.items():
            try:
                if str(company.get("ycdc_status") or "").lower() not in statuses:
                    continue
                lead = self._lead(slug, company, slug in hiring_slugs if hiring_known else None)
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "collector.posting_skipped",
                    extra={"collector": self.name, "target": slug, "error": str(exc)[:300]},
                )
                continue
            if lead is not None:
                leads.append(lead)
        return leads

    def _lead(
        self, slug: str, company: Mapping[str, Any], is_hiring: bool | None
    ) -> StartupLead | None:
        name = " ".join(str(company.get("name") or "").split())
        if not name:
            return None
        country = str(company.get("country") or "").upper()
        if country and country != "IN":  # the listing is India-HQ; guard against drift
            return None
        domain: str | None = None
        website = company.get("website")
        if isinstance(website, str) and website.strip():
            try:
                domain = canonical_domain(website)
            except ValueError:
                domain = None
            if domain and host_in(domain, self.non_company_hosts):
                domain = None
        city = str(company.get("city") or company.get("location") or "").strip()
        location = self.geo.resolve(f"{city}, India" if city else "India")
        tags = [str(t) for t in company.get("tags") or [] if isinstance(t, str)][:MAX_TAGS]
        team_size = company.get("team_size")
        lead = StartupLead(
            name=name,
            domain=domain,
            source=self.name,
            source_url=COMPANY_URL.format(slug=slug),
            sector=", ".join(tags) or None,
            batch=str(company.get("batch_name") or "").strip() or None,
            is_hiring=is_hiring,
            team_size=team_size if isinstance(team_size, int) and team_size > 0 else None,
            github_org=_github_org(company.get("github_url")),
        )
        return lead.with_hq(location) if location.country else lead
