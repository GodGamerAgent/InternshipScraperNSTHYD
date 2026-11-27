"""T-Hub (Hyderabad) incubator directories -> ``StartupLead``s.

Two page kinds (``sources.yaml: incubators``):

* ``tablepress`` — Lab 32 directory (``programs.t-hub.co/lab-32-directory/``): server-rendered
  TablePress tables, one per cohort, columns S.No / Startup Name / Website / Founder Name /
  Sector / Headquarters (verified 2026-11-26).
* ``webflow`` — main startups directory (``www.t-hub.co/startups-directory``): a Webflow page
  whose list is **rendered by JavaScript** (no ``w-dyn-item`` in the HTML on 2026-11-26). It is
  only parsed when a renderer (Firecrawl) is configured; otherwise the target fails with
  ``JavaScriptRequiredError`` so the run report says why. The card selectors below assume
  Webflow's standard CMS markup — re-verify them the first time a Firecrawl key is available.

Both pages are HTML, so robots.txt is always respected. Founder names are public on the page and
are kept only for Pass 3 contact patterns. ``default_city`` (hyderabad) applies **only** when a
row has no Headquarters value — many cohort startups are based elsewhere.
"""

import re
from collections.abc import Iterable
from typing import ClassVar
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from src.collectors.config import IncubatorConfig
from src.core.base import BaseCollector
from src.core.firecrawl_client import HtmlRenderer
from src.core.http import FetchError, HttpClient
from src.core.urls import canonical_domain, host_in
from src.enrichment.geo import GeoResolver
from src.models import StartupLead

FOUNDER_SPLIT = re.compile(r"\s*(?:,|&|/|;|\band\b)\s*", re.IGNORECASE)
_COLUMNS = {
    "name": ("startup name", "startup", "company", "name"),
    "website": ("website", "url", "web"),
    "founders": ("founder name", "founders", "founder"),
    "sector": ("sector", "industry", "domain"),
    "hq": ("headquarters", "hq", "location", "city"),
}


class JavaScriptRequiredError(FetchError):
    """The page needs JavaScript and no renderer (FIRECRAWL_API_KEY) is configured."""

    def __init__(self, url: str) -> None:
        super().__init__(url, None, "page is rendered by JavaScript; set FIRECRAWL_API_KEY")


def website_domain(cell_text: str, hrefs: list[str], blocked: list[str]) -> str | None:
    """The startup's own domain from a directory cell, skipping registries/social/job boards."""
    for candidate in [*hrefs, cell_text]:
        candidate = candidate.strip()
        if not candidate or " " in candidate:
            continue
        try:
            domain = canonical_domain(candidate)
        except ValueError:
            continue
        if not host_in(domain, blocked):
            return domain
    return None


def _column_index(headers: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for i, header in enumerate(h.lower().strip() for h in headers):
        for field, names in _COLUMNS.items():
            if field not in index and header in names:
                index[field] = i
    return index


def webflow_items(html: str) -> list[Tag]:
    """Webflow CMS list items (``.w-dyn-item``) present in ``html``."""
    return BeautifulSoup(html, "lxml").select(".w-dyn-item")


class ThubCollector(BaseCollector[IncubatorConfig, str, StartupLead]):
    """Incubator directory collector. ``renderer`` is the optional Firecrawl fallback."""

    name = "thub"
    kinds: ClassVar[tuple[str, ...]] = ("tablepress", "webflow")

    def __init__(
        self,
        http: HttpClient,
        *,
        incubators: list[IncubatorConfig],
        geo: GeoResolver,
        renderer: HtmlRenderer | None = None,
        non_company_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(http)
        self.non_company_hosts = list(non_company_hosts or [])
        self.incubators = [i for i in incubators if i.kind in self.kinds]
        self.geo = geo
        self.renderer = renderer

    def targets(self) -> list[IncubatorConfig]:
        return list(self.incubators)

    def describe_target(self, target: IncubatorConfig) -> str:
        return target.name

    async def fetch(self, target: IncubatorConfig) -> str:
        html = await self.http.get_text(target.url)  # HTML page: robots.txt respected
        if target.kind == "webflow" and not webflow_items(html):
            if self.renderer is None:
                raise JavaScriptRequiredError(target.url)
            html = await self.renderer.scrape_html(target.url)
        return html

    def parse(self, raw: str, target: IncubatorConfig) -> Iterable[StartupLead]:
        rows = (
            self._tablepress_rows(raw) if target.kind == "tablepress" else self._webflow_rows(raw)
        )
        own_host = ".".join(canonical_domain(target.url).split(".")[-2:])  # "t-hub.co"
        blocked = [*self.non_company_hosts, own_host]
        leads: list[StartupLead] = []
        for row in rows:
            name = " ".join(row.get("name", "").split())
            if not name:
                continue
            hq = row.get("hq", "").strip()
            location = (
                self.geo.resolve(hq)
                if hq
                else self.geo.resolve(None, source_default_city=target.default_city)
            )
            founders = [f for f in FOUNDER_SPLIT.split(row.get("founders", "")) if f.strip()]
            lead = StartupLead(
                name=name,
                domain=website_domain(row.get("website", ""), row.get("hrefs", []), blocked),
                source=target.name,
                source_url=f"{target.url}#{row['anchor']}" if row.get("anchor") else target.url,
                sector=row.get("sector") or None,
                founders=founders,
            )
            leads.append(lead.with_hq(location) if location.country else lead)
        return leads

    # -------------------------------------------------------------- page shapes
    @staticmethod
    def _tablepress_rows(html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        rows: list[dict] = []
        tables = soup.select("table.tablepress") or soup.find_all("table")
        for table in tables:
            headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
            index = _column_index(headers)
            if "name" not in index:
                continue
            for tr in table.find_all("tr"):
                cells = tr.find_all("td")
                if len(cells) < len(headers):
                    continue
                row: dict = {"anchor": table.get("id")}
                for field, i in index.items():
                    row[field] = cells[i].get_text(" ", strip=True)
                if "website" in index:
                    row["hrefs"] = [
                        a["href"] for a in cells[index["website"]].find_all("a", href=True)
                    ]
                rows.append(row)
        return rows

    @staticmethod
    def _webflow_rows(html: str) -> list[dict]:
        rows: list[dict] = []
        for item in webflow_items(html):
            heading = item.find(["h1", "h2", "h3", "h4", "h5", "h6"]) or item.select_one(
                "[class*=name], [class*=title]"
            )

            def text_of(selector: str, node: Tag = item) -> str:
                found = node.select_one(selector)
                return found.get_text(" ", strip=True) if found else ""

            hrefs = [
                a["href"]
                for a in item.find_all("a", href=True)
                if urlsplit(a["href"]).scheme in {"http", "https"}
            ]
            rows.append(
                {
                    "name": heading.get_text(" ", strip=True) if heading else "",
                    "website": "",
                    "hrefs": hrefs,
                    "sector": text_of("[class*=sector], [class*=industry], [class*=category]"),
                    "hq": text_of("[class*=location], [class*=city], [class*=headquarter]"),
                    "founders": text_of("[class*=founder]"),
                }
            )
        return rows
