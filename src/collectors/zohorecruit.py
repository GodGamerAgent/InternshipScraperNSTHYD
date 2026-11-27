"""Zoho Recruit public careers-portal collector (India-popular ATS, ADR 008).

Portal: ``GET https://{portal-host}/jobs/Careers`` where the seed ``slug`` is the **full portal
host** (``acme.zohorecruit.in`` or ``acme.zohorecruit.com`` — the data centre differs per
tenant, so the TLD can't be guessed). Shape notes from live portals (2026-11-26):

* The page is server-rendered HTML that embeds every published opening as a JSON array in
  ``<input type="hidden" id="jobs" value="[…]">`` (HTML-escaped attribute).
* Fields: ``id``, ``Posting_Title``, ``City``, ``State``, ``Country``, ``Remote_Job`` (bool),
  ``Job_Type`` ("Full time", "Internship", …), ``Job_Description`` (HTML), ``Date_Opened``
  (``YYYY-MM-DD``), ``Publish`` (bool). Unpublished openings are skipped.
* Job page: ``https://{host}/jobs/Careers/{id}``.

Unlike the Tier 1 JSON APIs this is an HTML page, so **robots.txt is respected** (Zoho's robots
only disallows authenticated paths). Everything India-specific (internship filter, geo, stipend)
is inherited from :class:`AtsCollector`.
"""

import json
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import quote

from bs4 import BeautifulSoup

from src.collectors.ats_base import AtsCollector, AtsJob, clean, parse_timestamp, require_list
from src.collectors.config import ZOHO_PORTAL_HOST, CompanySeed
from src.core.text import html_to_text
from src.models import AtsProvider

PORTAL_KEY = "_portal_url"  # added by ``postings`` so ``to_job`` can build absolute job URLs


def embedded_jobs(html: str) -> list[Mapping[str, Any]]:
    """The job dicts embedded in a portal page. ``ValueError`` if the ``jobs`` input is missing."""
    node = BeautifulSoup(html, "lxml").find("input", id="jobs")
    value = node.get("value") if node is not None else None
    if not isinstance(value, str):
        raise ValueError("no <input id='jobs'> found: not a Zoho Recruit careers page")
    try:
        data = json.loads(value) if value.strip() else []
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed jobs JSON: {exc}") from exc
    return require_list(data, "jobs")


def _join(*parts: Any) -> str | None:
    return ", ".join(dict.fromkeys(p for p in map(clean, parts) if p)) or None


class ZohoRecruitCollector(AtsCollector):
    name = "zohorecruit"
    provider: ClassVar[AtsProvider] = AtsProvider.ZOHORECRUIT

    async def fetch(self, target: CompanySeed) -> dict[str, str]:
        if not ZOHO_PORTAL_HOST.fullmatch(target.slug.lower()):  # e.g. a malformed DB seed
            raise ValueError(f"not a Zoho Recruit portal host: {target.slug!r}")
        url = self.source.endpoint.format(slug=quote(target.slug, safe="."))
        # HTML careers page (not a documented API): robots.txt is respected.
        return {"url": url, "html": await self.http.get_text(url)}

    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("html"), str):
            raise ValueError("expected {'url': portal URL, 'html': careers page}")
        portal = str(raw["url"]).rstrip("/")
        return [
            {**job, PORTAL_KEY: portal}
            for job in embedded_jobs(raw["html"])
            if job.get("Publish") is not False
        ]

    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        job_id = str(posting["id"]).strip()
        if not job_id.isdigit():
            raise ValueError(f"unexpected job id {job_id!r}")
        location = _join(posting.get("City"), posting.get("State"), posting.get("Country"))
        return AtsJob(
            title=str(posting["Posting_Title"]).strip(),
            url=f"{posting[PORTAL_KEY]}/{job_id}",
            locations=(location,) if location else (),
            employment_type=clean(posting.get("Job_Type")),
            workplace="remote" if posting.get("Remote_Job") is True else None,
            country_code="IN" if (clean(posting.get("Country")) or "").lower() == "india" else None,
            description=html_to_text(posting.get("Job_Description")),
            posted_at=parse_timestamp(posting.get("Date_Opened")),
        )
