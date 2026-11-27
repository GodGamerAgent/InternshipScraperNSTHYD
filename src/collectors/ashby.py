"""Ashby Public Job Posting API collector.

Endpoint: ``GET https://api.ashbyhq.com/posting-api/job-board/{jobs_page_name}?includeCompensation=true``
(https://developers.ashbyhq.com/docs/public-job-posting-api).

Shape notes from live boards (2026-11):

* ``employmentType`` is ``FullTime`` / ``Intern`` / … — but some internships are labelled
  ``FullTime``, so the title matcher still matters.
* ``workplaceType`` is ``OnSite`` / ``Hybrid`` / ``Remote`` / ``null``; ``isRemote`` is a fallback.
* ``secondaryLocations[].location`` holds extra cities; ``address.postalAddress.addressCountry``
  is a country *name* (``"India"``).
* Compensation: ``compensation.summaryComponents[]`` with ``interval: "1 MONTH"`` and
  ``currencyCode`` — only INR monthly salary components become a stipend.
* ``isListed: false`` postings are unlisted and skipped.
"""

from collections.abc import Mapping
from typing import Any, ClassVar

from src.collectors.ats_base import (
    AtsCollector,
    AtsJob,
    Workplace,
    clean,
    parse_timestamp,
    require_list,
)
from src.core.text import html_to_text
from src.enrichment.stipend import Stipend, stipend_from_structured
from src.models import AtsProvider

_WORKPLACE: dict[str, Workplace] = {"onsite": "onsite", "hybrid": "hybrid", "remote": "remote"}


def _stipend(compensation: Mapping[str, Any] | None) -> Stipend | None:
    for component in (compensation or {}).get("summaryComponents") or []:
        if str(component.get("compensationType", "")).lower() not in {"salary", "stipend"}:
            continue
        stipend = stipend_from_structured(
            component.get("minValue"),
            component.get("maxValue"),
            component.get("currencyCode"),
            component.get("interval"),
        )
        if stipend is not None:
            return stipend
    return None


class AshbyCollector(AtsCollector):
    name = "ashby"
    provider: ClassVar[AtsProvider] = AtsProvider.ASHBY

    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        if not isinstance(raw, Mapping):
            raise ValueError("expected a JSON object with a 'jobs' list")
        jobs = require_list(raw.get("jobs", []), "jobs")
        return [job for job in jobs if job.get("isListed", True) is not False]

    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        workplace = _WORKPLACE.get(str(posting.get("workplaceType") or "").lower())
        if workplace is None and posting.get("isRemote") is True:
            workplace = "remote"
        secondary = [loc.get("location") for loc in posting.get("secondaryLocations") or []]
        country = clean(
            ((posting.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        )
        return AtsJob(
            title=posting["title"].strip(),
            url=posting["jobUrl"],
            locations=tuple(x for x in map(clean, [posting.get("location"), *secondary]) if x),
            employment_type=clean(posting.get("employmentType")),
            workplace=workplace,
            country_code="IN" if country and country.lower() == "india" else country,
            description=clean(posting.get("descriptionPlain"))
            or html_to_text(posting.get("descriptionHtml")),
            posted_at=parse_timestamp(posting.get("publishedAt")),
            stipend=_stipend(posting.get("compensation")),
        )
