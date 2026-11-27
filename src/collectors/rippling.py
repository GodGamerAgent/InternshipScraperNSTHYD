"""Rippling ATS public job-board collector (ADR 009).

Endpoint: ``GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs`` (keyless; the
JSON behind ``https://ats.rippling.com/{slug}/jobs``). Shape from live boards (2026-11-26):

* A JSON **list** of ``{uuid, name, department: {id, label}, url, workLocation: {id, label}}``.
  The list has no employment type or description, so the internship filter runs on the title.
* ``workLocation.label`` examples: ``"Bengaluru, India"``, ``"Remote, India"``,
  ``"Hybrid (Bengaluru, Karnataka, IN)"``, ``"Remote (United States)"``.

``api.rippling.com`` has no robots.txt (404 → allowed, RFC 9309), and ``ats.rippling.com`` only
disallows ``/internal/``. The collector still keeps robots.txt **on** (ADR 009).
"""

from collections.abc import Mapping
from typing import Any, ClassVar

from src.collectors.ats_base import AtsCollector, AtsJob, Workplace, clean, require_list
from src.models import AtsProvider


def _workplace(label: str) -> Workplace | None:
    lowered = label.lower()
    if lowered.startswith("remote"):
        return "remote"
    if lowered.startswith("hybrid"):
        return "hybrid"
    return None


class RipplingCollector(AtsCollector):
    name = "rippling"
    provider: ClassVar[AtsProvider] = AtsProvider.RIPPLING
    respect_robots: ClassVar[bool] = True

    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        if isinstance(raw, Mapping):  # tolerate a future {"jobs": [...]} envelope
            raw = raw.get("jobs", raw.get("items"))
        return require_list(raw, "jobs")

    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        places = posting.get("workLocations") or [posting.get("workLocation")]
        labels = [
            clean(p.get("label")) if isinstance(p, Mapping) else clean(p) for p in places if p
        ]
        locations = tuple(x for x in labels if x)
        workplaces = {_workplace(x) for x in locations} - {None}
        return AtsJob(
            title=str(posting["name"]).strip(),
            url=posting["url"],
            locations=locations,
            workplace=workplaces.pop() if len(workplaces) == 1 else None,
        )
