"""Greenhouse Job Board API collector.

Endpoint: ``GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true``
(https://developers.greenhouse.io/job-board.html). Shape notes from live boards (2026-11):

* ``location.name`` is free text such as ``"Onsite in Bangalore"`` or
  ``"Hybrid in Bengaluru, India"`` — the geo resolver understands the city and the work-mode words.
* ``content`` is HTML that is itself HTML-escaped (``&lt;p&gt;``): unescape, then strip tags.
* There is no employment-type field; department names (e.g. ``"Internship"``) are used instead.
"""

import html
from collections.abc import Mapping
from typing import Any, ClassVar

from src.collectors.ats_base import AtsCollector, AtsJob, clean, parse_timestamp, require_list
from src.core.text import html_to_text
from src.models import AtsProvider


class GreenhouseCollector(AtsCollector):
    name = "greenhouse"
    provider: ClassVar[AtsProvider] = AtsProvider.GREENHOUSE

    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        if not isinstance(raw, Mapping):
            raise ValueError("expected a JSON object with a 'jobs' list")
        return require_list(raw.get("jobs", []), "jobs")

    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        location = clean((posting.get("location") or {}).get("name"))
        offices = [clean(o.get("name")) for o in posting.get("offices") or []]
        departments = " ".join(clean(d.get("name")) or "" for d in posting.get("departments") or [])
        return AtsJob(
            title=posting["title"].strip(),
            url=posting["absolute_url"],
            locations=tuple(x for x in [location, *([] if location else offices)] if x),
            employment_type=departments or None,
            description=html_to_text(html.unescape(posting.get("content") or "")),
            posted_at=parse_timestamp(posting.get("first_published") or posting.get("updated_at")),
        )
