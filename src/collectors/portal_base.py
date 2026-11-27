"""Shared helpers for Indian student portals (Internshala, Cutshort, Instahyre; ADR 011).

These portals list India roles only, so a bare "Work from home" / "Remote only" means remote
*within India*. ``GeoResolver`` alone would call that ``remote_global``. Anything that resolves
to a foreign country is dropped.
"""

from typing import Literal

from src.enrichment.geo import GeoResolver
from src.models import ResolutionMethod, ResolvedLocation

PortalWorkplace = Literal["onsite", "hybrid", "remote"]


def india_portal_location(
    geo: GeoResolver, raw: str, *, workplace: PortalWorkplace | None = None
) -> ResolvedLocation | None:
    """Resolve a portal location string, treating generic remote text as Remote-India."""
    text = " ".join(raw.split())
    query = text
    if workplace == "remote" and "remote" not in text.lower():
        query = f"{text} (Remote)".strip()
    elif workplace == "hybrid" and "hybrid" not in text.lower():
        query = f"{text} (Hybrid)".strip()
    if text and geo.resolve(text).resolution_method is ResolutionMethod.FOREIGN:
        return None  # an explicitly foreign place ("Dubai, UAE"), whatever the workplace flag
    first = geo.resolve(query or "Remote")
    if first.is_india:
        location = first
    else:  # generic remote text or an unknown (Indian) town: India-only portal
        location = geo.resolve(f"{query or 'Remote'} India")
        if not location.is_india:
            return None
    return location.model_copy(update={"raw_location": text[:300] or None})
