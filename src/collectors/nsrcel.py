"""NSRCEL (IIM Bangalore) portfolio page -> ``StartupLead``s (ADR 008).

Source (``sources.yaml: incubators``, ``kind: nsrcel_popup``): ``https://nsrcel.org/portfolio/``,
a server-rendered Elementor page (robots.txt allows it; respected). Verified 2026-11-26: each
startup is a container whose class ends in ``-startup-popup`` (23 ``fintech-coe-startup-popup``
blocks) holding

* ``img[alt]`` — the startup name,
* ``<p>`` — a description,
* ``<li>`` rows ``"Key: Value"`` — ``Founder``, ``Co-Founders``, ``Stage of Venture``,
  ``Sub-Sectors``, ``Website`` (with an ``<a href>``), ``Business Model``, ``Venture Age``.

There is no HQ field, so ``default_city`` (bengaluru) is applied to every row through
``GeoResolver``'s source-default rule. Founder names are public on the page and are kept only for
the Pass 3 contact patterns. If a future redesign moves the list behind JavaScript, the optional
Firecrawl renderer is used, else the target fails with ``JavaScriptRequiredError``.
"""

import re
from collections.abc import Iterable
from typing import ClassVar

from bs4 import BeautifulSoup, Tag

from src.collectors.config import IncubatorConfig
from src.collectors.thub import FOUNDER_SPLIT, JavaScriptRequiredError, website_domain
from src.core.base import BaseCollector
from src.core.firecrawl_client import HtmlRenderer
from src.core.http import HttpClient
from src.core.urls import canonical_domain
from src.enrichment.funding import detect_stage
from src.enrichment.geo import GeoResolver
from src.models import FundingStage, StartupLead

POPUP_SELECTOR = "[class*='-startup-popup']"
_KEY_VALUE = re.compile(r"^\s*(?P<key>[^:]{1,40}):\s*(?P<value>.*)$", re.DOTALL)


def popups(html: str) -> list[Tag]:
    """Startup popup containers (outermost only) present in ``html``."""
    found = BeautifulSoup(html, "lxml").select(POPUP_SELECTOR)
    return [tag for tag in found if not any(p in found for p in tag.parents)]


def popup_fields(popup: Tag) -> dict[str, object]:
    """``name``, ``description``, ``hrefs`` and lower-cased ``Key: Value`` rows of one popup."""
    image = popup.find("img", alt=True)
    heading = popup.find(["h2", "h3", "h4", "h5"])
    name = (image.get("alt") if image else "") or (
        heading.get_text(" ", strip=True) if heading else ""
    )
    rows: dict[str, object] = {"name": " ".join(str(name).split())}
    paragraph = popup.find("p")
    rows["description"] = paragraph.get_text(" ", strip=True) if paragraph else ""
    for item in popup.find_all("li"):
        match = _KEY_VALUE.match(item.get_text(" ", strip=True))
        if not match:
            continue
        key = " ".join(match["key"].lower().split())
        rows.setdefault(key, match["value"].strip())
        if key == "website":
            rows["hrefs"] = [a["href"] for a in item.find_all("a", href=True)]
    return rows


def venture_stage(text: str) -> FundingStage | None:
    """``Stage of Venture`` → stage ("Bootstrapped" is listed literally on this page)."""
    if re.search(r"\bbootstrap", text, re.IGNORECASE):
        return FundingStage.BOOTSTRAPPED
    return detect_stage(text)


class NsrcelCollector(BaseCollector[IncubatorConfig, str, StartupLead]):
    """NSRCEL portfolio popups (``kind: nsrcel_popup`` incubators only)."""

    name = "nsrcel"
    kinds: ClassVar[tuple[str, ...]] = ("nsrcel_popup",)

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
        self.incubators = [i for i in incubators if i.kind in self.kinds]
        self.geo = geo
        self.renderer = renderer
        self.non_company_hosts = list(non_company_hosts or [])

    def targets(self) -> list[IncubatorConfig]:
        return list(self.incubators)

    def describe_target(self, target: IncubatorConfig) -> str:
        return target.name

    async def fetch(self, target: IncubatorConfig) -> str:
        html = await self.http.get_text(target.url)  # HTML page: robots.txt respected
        if not popups(html):
            if self.renderer is None:
                raise JavaScriptRequiredError(target.url)
            html = await self.renderer.scrape_html(target.url)
        return html

    def parse(self, raw: str, target: IncubatorConfig) -> Iterable[StartupLead]:
        own_host = ".".join(canonical_domain(target.url).split(".")[-2:])  # "nsrcel.org"
        blocked = [*self.non_company_hosts, own_host]
        location = self.geo.resolve(None, source_default_city=target.default_city)
        leads: dict[str, StartupLead] = {}
        for popup in popups(raw):
            row = popup_fields(popup)
            name = str(row.get("name") or "")
            if not name:
                continue
            founders = [
                part
                for key in ("founder", "founders", "co-founder", "co-founders")
                for part in FOUNDER_SPLIT.split(str(row.get(key) or ""))
                if part.strip()
            ]
            hrefs = row.get("hrefs")
            sector = str(row.get("sub-sectors") or row.get("sector") or "").strip()
            lead = StartupLead(
                name=name,
                domain=website_domain(
                    str(row.get("website") or ""),
                    list(hrefs) if isinstance(hrefs, list) else [],
                    blocked,
                ),
                source=self.name,
                source_url=target.url,
                sector=sector[:200] or None,
                founders=founders,
                funding_stage=venture_stage(str(row.get("stage of venture") or "")),
            )
            lead = lead.with_hq(location) if location.country else lead
            leads.setdefault(lead.name_key, lead)
        return list(leads.values())
