"""Recruitee Careers Site API collector (ADR 009).

Endpoint: ``GET https://{slug}.recruitee.com/api/offers/`` (keyless, documented as the public
Careers Site API). Shape from live boards (2026-11-26):

* ``{"offers": [{id, slug, title, description (HTML), requirements (HTML), status, city,
  country, country_code, location, locations: [{name, city, state, country, country_code}],
  remote, hybrid, on_site (bools), employment_type_code, careers_url, published_at,
  company_name, salary: {min, max, period, currency}}]}``.
* ``employment_type_code`` values: ``internship``, ``fulltime_permanent``,
  ``fulltime_fixed_term``, ``contract``, ``freelance``…
* ``published_at`` looks like ``"2026-11-12 06:42:00 UTC"``.
* Only ``status == "published"`` offers are kept. ``salary`` becomes a stipend only for INR
  monthly figures.

Tenant subdomains serve their own robots.txt, which is respected (ADR 009).
"""

from collections.abc import Mapping
from typing import Any, ClassVar

from src.collectors.ats_base import AtsCollector, AtsJob, clean, parse_timestamp, require_list
from src.core.text import html_to_text
from src.enrichment.stipend import stipend_from_structured
from src.models import AtsProvider


def _place(location: Mapping[str, Any]) -> str | None:
    parts = [clean(location.get(k)) for k in ("city", "state", "country")]
    return ", ".join(dict.fromkeys(p for p in parts if p)) or clean(location.get("name"))


class RecruiteeCollector(AtsCollector):
    name = "recruitee"
    provider: ClassVar[AtsProvider] = AtsProvider.RECRUITEE
    respect_robots: ClassVar[bool] = True

    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        if not isinstance(raw, Mapping):
            raise ValueError("expected a JSON object with an 'offers' list")
        offers = require_list(raw.get("offers", []), "offers")
        return [o for o in offers if str(o.get("status") or "published") == "published"]

    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        places = [p for p in posting.get("locations") or [] if isinstance(p, Mapping)]
        locations = tuple(dict.fromkeys(x for x in map(_place, places) if x)) or tuple(
            x for x in [clean(posting.get("location"))] if x
        )
        if posting.get("remote") is True and not posting.get("on_site"):
            workplace = "remote"
        elif posting.get("hybrid") is True:
            workplace = "hybrid"
        else:
            workplace = None
        salary = posting.get("salary") if isinstance(posting.get("salary"), Mapping) else {}
        published = str(posting.get("published_at") or "").removesuffix(" UTC") or None
        return AtsJob(
            title=str(posting["title"]).strip(),
            url=posting["careers_url"],
            locations=locations,
            employment_type=clean(posting.get("employment_type_code")),
            workplace=workplace,
            country_code=clean(posting.get("country_code")),
            description=html_to_text(
                f"{posting.get('description') or ''}\n{posting.get('requirements') or ''}"
            ),
            posted_at=parse_timestamp(published),
            stipend=stipend_from_structured(
                _number(salary.get("min")),
                _number(salary.get("max")),
                salary.get("currency"),
                salary.get("period"),
            ),
        )


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
