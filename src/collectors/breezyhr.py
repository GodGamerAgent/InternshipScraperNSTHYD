"""BreezyHR public careers-portal JSON collector (ADR 009).

Endpoint: ``GET https://{slug}.breezy.hr/json`` (keyless; the portal's own published-positions
feed). Shape from live portals (2026-11-26):

* A JSON **list** of ``{id, friendly_id, name, url, published_date, type: {id, name},
  location: {city, state, country: {id, name}, is_remote, name}, locations: [...],
  department, salary, company: {name, friendly_id}}``.
* ``type.id``: ``fullTime``, ``partTime``, ``contract``, ``temporary``, ``internship``…
* ``salary`` is free text (``"₹500,000 – ₹800,000 / year"``, often ``""``). It is passed to
  the stipend parser as description text.

Each tenant subdomain serves its own robots.txt, which is respected (ADR 009). The
``breezy.hr`` marketing site only disallows its ``/api/`` and app paths.
"""

from collections.abc import Mapping
from typing import Any, ClassVar

from src.collectors.ats_base import AtsCollector, AtsJob, clean, parse_timestamp, require_list
from src.models import AtsProvider


def _place(location: Mapping[str, Any]) -> str | None:
    country = location.get("country")
    country_name = clean(country.get("name")) if isinstance(country, Mapping) else None
    name = clean(location.get("name"))
    if name and country_name and country_name.lower() not in name.lower():
        return f"{name}, {country_name}"
    return name or country_name


class BreezyHrCollector(AtsCollector):
    name = "breezyhr"
    provider: ClassVar[AtsProvider] = AtsProvider.BREEZYHR
    respect_robots: ClassVar[bool] = True

    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        return require_list(raw, "positions")

    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        primary = posting.get("location") if isinstance(posting.get("location"), Mapping) else {}
        places = [p for p in posting.get("locations") or [] if isinstance(p, Mapping)] or [primary]
        locations = tuple(dict.fromkeys(x for x in map(_place, places) if x))
        country = primary.get("country") if isinstance(primary, Mapping) else None
        kind = posting.get("type") if isinstance(posting.get("type"), Mapping) else {}
        remote = bool(places) and all(p.get("is_remote") is True for p in places)
        return AtsJob(
            title=str(posting["name"]).strip(),
            url=posting["url"],
            locations=locations,
            employment_type=clean(kind.get("name")) or clean(kind.get("id")),
            workplace="remote" if remote else None,
            country_code=clean(country.get("id")) if isinstance(country, Mapping) else None,
            description=clean(posting.get("salary")) or "",
            posted_at=parse_timestamp(posting.get("published_date")),
        )
