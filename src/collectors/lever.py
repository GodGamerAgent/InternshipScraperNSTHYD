"""Lever Postings API collector.

Endpoint: ``GET https://api.lever.co/v0/postings/{company}?mode=json``
(https://github.com/lever/postings-api). Shape notes from live boards (2026-11):

* ``categories.commitment`` is the employment type (``"Intern"``, ``"Full-time Employment"``…).
* ``categories.allLocations`` lists every location; ``country`` is ISO-2 (``"IN"``).
* ``workplaceType`` is ``onsite`` / ``hybrid`` / ``remote`` / ``unspecified``.
* ``salaryRange`` is often ``{min: 0, max: 0}`` (undisclosed) — zeros are ignored.
* An unknown company returns HTTP 404 (surfaces as a ``fetch`` issue for that target).
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
from src.enrichment.stipend import stipend_from_structured
from src.models import AtsProvider

_WORKPLACE: dict[str, Workplace] = {"onsite": "onsite", "hybrid": "hybrid", "remote": "remote"}


class LeverCollector(AtsCollector):
    name = "lever"
    provider: ClassVar[AtsProvider] = AtsProvider.LEVER

    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        return require_list(raw, "postings")

    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        categories = posting.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location")]
        sections = [
            clean(posting.get("descriptionPlain")) or "",
            *(
                f"{clean(item.get('text')) or ''}\n{html_to_text(item.get('content'))}"
                for item in posting.get("lists") or []
            ),
            clean(posting.get("additionalPlain")) or "",
        ]
        salary = posting.get("salaryRange") or {}
        return AtsJob(
            title=posting["text"].strip(),
            url=posting["hostedUrl"],
            locations=tuple(x for x in map(clean, locations) if x),
            employment_type=clean(categories.get("commitment")),
            workplace=_WORKPLACE.get(str(posting.get("workplaceType") or "").lower()),
            country_code=clean(posting.get("country")),
            description="\n".join(s for s in sections if s.strip()),
            posted_at=parse_timestamp(posting.get("createdAt")),
            stipend=stipend_from_structured(
                salary.get("min"), salary.get("max"), salary.get("currency"), salary.get("interval")
            ),
        )
