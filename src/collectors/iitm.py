"""IIT Madras Incubation Cell portfolio (Chennai) -> ``StartupLead``s (ADR 008).

Source (``sources.yaml: incubators``, ``kind: iitm_api``): the public JSON API behind the
incubator's own portfolio page,
``GET https://incubation.iitm.ac.in/api/portfolios/active?page=N&limit=100``. Shape verified
2026-11-26: ``{"data": [...], "pagination": {"page", "limit", "total", "totalPages",
"hasMore"}}`` with 425 portfolios on 5 pages of 100. Useful fields: ``company_name``,
``website_url``, ``founders[].name``, ``primary_sector`` / ``industry`` / ``category`` (mostly
empty), ``location`` (``["India"]`` or ``null``), ``is_active``.

It is not a documented third-party API, so robots.txt **is** respected (the site serves its SPA
shell for ``/robots.txt``, i.e. no rules). Records carry no city, so ``default_city`` (chennai)
applies whenever a row's location is missing or just "India" — ``GeoResolver`` only uses the
source default when nothing more specific resolves.
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

from src.collectors.config import IncubatorConfig
from src.collectors.thub import website_domain
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.core.urls import canonical_domain
from src.enrichment.geo import GeoResolver
from src.models import StartupLead

logger = logging.getLogger(__name__)


def portfolio_rows(page: Any) -> list[Mapping[str, Any]]:
    """``data[]`` of one API page. ``ValueError`` on an unexpected shape."""
    data = page.get("data") if isinstance(page, Mapping) else None
    if not isinstance(data, list):
        raise ValueError("expected {'data': [...]} from the IITM portfolio API")
    return [row for row in data if isinstance(row, Mapping)]


class IitmCollector(BaseCollector[IncubatorConfig, list[Any], StartupLead]):
    """Paginated IITM portfolio API (``kind: iitm_api`` incubators only)."""

    name = "iitm"
    kinds: ClassVar[tuple[str, ...]] = ("iitm_api",)

    def __init__(
        self,
        http: HttpClient,
        *,
        incubators: list[IncubatorConfig],
        geo: GeoResolver,
        non_company_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(http)
        self.incubators = [i for i in incubators if i.kind in self.kinds]
        self.geo = geo
        self.non_company_hosts = list(non_company_hosts or [])

    def targets(self) -> list[IncubatorConfig]:
        return list(self.incubators)

    def describe_target(self, target: IncubatorConfig) -> str:
        return target.name

    async def fetch(self, target: IncubatorConfig) -> list[Any]:
        pages: list[Any] = []
        for page in range(1, target.max_pages + 1):
            # The incubator's own site API (undocumented): robots.txt respected (default).
            reply = await self.http.get_json(
                target.url, params={"page": page, "limit": target.page_size}
            )
            pages.append(reply)
            pagination = reply.get("pagination") if isinstance(reply, Mapping) else None
            if not isinstance(pagination, Mapping) or not pagination.get("hasMore"):
                break
            if not portfolio_rows(reply):
                break
        else:
            logger.warning(
                "iitm.max_pages_reached", extra={"target": target.name, "pages": target.max_pages}
            )
        return pages

    def parse(self, raw: list[Any], target: IncubatorConfig) -> Iterable[StartupLead]:
        parts = urlsplit(target.url)
        site = f"{parts.scheme}://{parts.netloc}/"
        own_host = ".".join(canonical_domain(target.url).split(".")[-3:])  # "iitm.ac.in"
        blocked = [*self.non_company_hosts, own_host]
        leads: dict[str, StartupLead] = {}
        for page in raw:
            for row in portfolio_rows(page):
                try:
                    lead = self._lead(row, target, site, blocked)
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "collector.posting_skipped",
                        extra={"collector": self.name, "target": target.name,
                               "error": str(exc)[:300]},
                    )  # fmt: skip
                    continue
                if lead is not None:
                    leads.setdefault(lead.name_key, lead)
        return list(leads.values())

    def _lead(
        self, row: Mapping[str, Any], target: IncubatorConfig, site: str, blocked: list[str]
    ) -> StartupLead | None:
        if row.get("is_active") is False:
            return None
        name = " ".join(str(row.get("company_name") or "").split())
        if not name:
            return None
        website = row.get("website_url")
        domain = website_domain(website, [], blocked) if isinstance(website, str) else None
        founders = [
            " ".join(str(f.get("name")).split())
            for f in row.get("founders") or []
            if isinstance(f, Mapping) and isinstance(f.get("name"), str) and f["name"].strip()
        ]
        sector = next(
            (str(row[k]).strip() for k in ("primary_sector", "industry", "category")
             if isinstance(row.get(k), str) and row[k].strip()),
            None,
        )  # fmt: skip
        places = row.get("location")
        raw_location = (
            ", ".join(p for p in places if isinstance(p, str) and p.strip())
            if isinstance(places, list)
            else (places if isinstance(places, str) else "")
        )
        location = self.geo.resolve(raw_location or None, source_default_city=target.default_city)
        lead = StartupLead(
            name=name,
            domain=domain,
            source=self.name,
            source_url=site,
            sector=sector[:200] if sector else None,
            founders=founders,
        )
        return lead.with_hq(location) if location.country else lead
