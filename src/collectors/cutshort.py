"""Cutshort public job listings, internships only (ADR 011).

Pages (``sources.yaml: student_portals.cutshort``): listing URLs taken from Cutshort's own
``sitemap_listings.xml``, e.g. ``/jobs/startup-jobs-in-hyderabad`` and ``/jobs/remote-jobs``.
Page 1 of each is fetched with robots.txt respected. Detail paths (``/view/j/``, ``/vj/``,
``/apply/``) are disallowed and never fetched.

Data, verified 2026-11-26: a Next.js page whose ``script#__NEXT_DATA__`` holds
``props.pageProps.dehydratedState.queries[].state.data.data.pageData.jobs[]`` (50 per page). Each
job has ``headline``, ``publicUrl``, ``roleTypes`` (``["internship"]``, ``["full_time"]``…),
``remoteType`` (``remote_only`` / …), ``locationsText``, ``salaryRangeText`` ("₹3000 - ₹5000 /
mo"), and ``companyDetails`` with ``name`` and ``links.website``.

Only roles whose ``roleTypes`` include ``internship`` are kept. The company website
published by Cutshort becomes the ``Startup``. The recruiter fields (``createdBy``) are personal
data and are **never read**.

**Terms of Service:** Cutshort's Terms prohibit automated access and scraping. The project owner
chose to collect public listings anyway (ADR 011). The collector stays polite and never bypasses
logins, CAPTCHAs or firewalls.
"""

import json
import logging
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from bs4 import BeautifulSoup
from pydantic import ValidationError

from src.collectors.config import CutshortConfig
from src.collectors.portal_base import PortalWorkplace, india_portal_location
from src.collectors.remote_base import remote_opportunity
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.core.urls import canonical_domain, host_in
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import CollectedOpportunity, Startup

logger = logging.getLogger(__name__)

_WORKPLACE: dict[str, PortalWorkplace] = {"remote_only": "remote", "hybrid": "hybrid"}


def page_jobs(html: str) -> list[Mapping[str, Any]]:
    """Every ``pageData.jobs`` list in the page's ``__NEXT_DATA__``. ``ValueError`` if absent."""
    node = BeautifulSoup(html, "lxml").find("script", id="__NEXT_DATA__")
    if node is None:
        raise ValueError("no __NEXT_DATA__ script: Cutshort layout changed")
    try:
        data = json.loads(node.get_text())
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"unexpected __NEXT_DATA__ shape: {exc}") from exc
    jobs: list[Mapping[str, Any]] = []
    for query in queries if isinstance(queries, list) else []:
        try:
            found = query["state"]["data"]["data"]["pageData"]["jobs"]
        except (KeyError, TypeError):
            continue
        jobs.extend(j for j in found if isinstance(j, Mapping))
    return jobs


class CutshortCollector(BaseCollector[CutshortConfig, list[Any], CollectedOpportunity]):
    """Single target: every selected city/remote listing (page 1 each)."""

    name = "cutshort"

    def __init__(
        self,
        http: HttpClient,
        *,
        config: CutshortConfig | None,
        geo: GeoResolver,
        matcher: InternshipMatcher,
        non_company_hosts: list[str] | None = None,
        cities: Collection[str] | None = None,
    ) -> None:
        super().__init__(http)
        self.config = config
        self.geo = geo
        self.matcher = matcher
        self.non_company_hosts = list(non_company_hosts or [])
        self.cities = set(cities) if cities is not None else None

    def selected(self, config: CutshortConfig) -> dict[str, str]:
        """Targets narrowed by ``--cities``; ``remote`` is always kept."""
        if self.cities is None:
            return dict(config.targets)
        return {k: v for k, v in config.targets.items() if k == "remote" or k in self.cities}

    def targets(self) -> list[CutshortConfig]:
        return [self.config] if self.config is not None and self.selected(self.config) else []

    def describe_target(self, target: CutshortConfig) -> str:
        return ",".join(self.selected(target))

    def close_scope(self, target: CutshortConfig) -> str | None:
        return "*"

    async def fetch(self, target: CutshortConfig) -> list[Any]:
        # HTML listing pages: robots.txt respected (default).
        return [
            [key, await self.http.get_text(f"{target.base_url}{path}")]
            for key, path in self.selected(target).items()
        ]

    def parse(self, raw: list[Any], target: CutshortConfig) -> Iterable[CollectedOpportunity]:
        items: dict[str, CollectedOpportunity] = {}
        for key, html in raw:
            for job in page_jobs(html):
                try:
                    item = self._item(job)
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    logger.warning(
                        "collector.posting_skipped",
                        extra={"collector": self.name, "target": key,
                               "error_type": type(exc).__name__, "error": str(exc)[:300]},
                    )  # fmt: skip
                    continue
                if item is not None:
                    items.setdefault(item.opportunity.url, item)
        return list(items.values())

    def _startup(self, company: Mapping[str, Any]) -> Startup | None:
        links = company.get("links") if isinstance(company.get("links"), Mapping) else {}
        website = links.get("website")
        name = str(company.get("name") or "").strip()
        if not isinstance(website, str) or not name:
            return None
        try:
            domain = canonical_domain(website)
        except ValueError:
            return None
        if host_in(domain, self.non_company_hosts):
            return None
        return Startup(
            name=name[:200],
            canonical_domain=domain,
            website_url=f"https://{domain}",
            discovered_via=self.name,
        )

    def _item(self, job: Mapping[str, Any]) -> CollectedOpportunity | None:
        title = " ".join(str(job["headline"]).split())
        roles = [str(r) for r in job.get("roleTypes") or []]
        if "internship" not in roles and not self.matcher.is_internship(title):
            return None
        location = india_portal_location(
            self.geo,
            str(job.get("locationsText") or ""),
            workplace=_WORKPLACE.get(str(job.get("remoteType") or "")),
        )
        if location is None:
            return None
        company = (
            job.get("companyDetails") if isinstance(job.get("companyDetails"), Mapping) else {}
        )
        salary = str(job.get("salaryRangeText") or "")
        details = [str(company.get("name") or ""), f"Stipend: {salary}" if salary else ""]
        return remote_opportunity(
            source=self.name,
            title=title,
            url=str(job["publicUrl"]),
            description="\n".join(d for d in details if d),
            location=location,
            posted_at=None,
            startup=self._startup(company),
        )
