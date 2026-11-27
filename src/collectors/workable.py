"""Workable public careers-widget collector.

Endpoint: ``GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true``
(keyless; ``details=true`` adds ``description``). Shape notes from live accounts (2026-11):

* Unknown account -> HTTP 404; a real account with no openings -> ``{"name": …, "jobs": []}``.
* ``employment_type`` (``"Full-time"``, ``"Internship"``…), ``telecommuting`` (bool).
* ``locations[]`` = ``{city, region, country, countryCode, hidden}``; top-level ``city`` /
  ``state`` / ``country`` mirror the first location.
* ``published_on`` is a plain date (``"2026-08-04"``).
"""

from collections.abc import Mapping
from typing import Any, ClassVar

from src.collectors.ats_base import AtsCollector, AtsJob, clean, parse_timestamp, require_list
from src.core.text import html_to_text
from src.models import AtsProvider


def _join(*parts: Any) -> str | None:
    return ", ".join(p for p in map(clean, parts) if p) or None


class WorkableCollector(AtsCollector):
    name = "workable"
    provider: ClassVar[AtsProvider] = AtsProvider.WORKABLE

    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        if not isinstance(raw, Mapping):
            raise ValueError("expected a JSON object with a 'jobs' list")
        return require_list(raw.get("jobs", []), "jobs")

    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        visible = [loc for loc in posting.get("locations") or [] if not loc.get("hidden")]
        locations = [
            _join(loc.get("city"), loc.get("region"), loc.get("country")) for loc in visible
        ]
        if not any(locations):
            locations = [_join(posting.get("city"), posting.get("state"), posting.get("country"))]
        country_code = clean(visible[0].get("countryCode")) if visible else None
        return AtsJob(
            title=posting["title"].strip(),
            url=posting.get("url") or posting["shortlink"],
            locations=tuple(x for x in locations if x),
            employment_type=clean(posting.get("employment_type")),
            workplace="remote" if posting.get("telecommuting") is True else None,
            country_code=country_code,
            description=html_to_text(posting.get("description")),
            posted_at=parse_timestamp(posting.get("published_on") or posting.get("created_at")),
        )
