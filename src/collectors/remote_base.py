"""Shared rules for remote-job feeds (Himalayas, Remotive; ADR 010).

The question for every remote posting is "can a student in India take this?" It is answered
the same way for every feed, from ``sources.yaml: remote_feeds``:

* **India**: the role is restricted to India (``india_country_names``), or its only timezone is
  IST (``india_utc_offset``) → ``remote_india``.
* **Worldwide**: there is no country restriction (and the timezones, if any, include IST), or the
  free-text location contains a ``worldwide_keywords`` term → ``remote_global``, kept only if
  ``keep_worldwide`` is set.
* **Anything else** (US-only, "Europe", "LATAM", …) is dropped.

``GeoResolver`` turns the decision into a ``ResolvedLocation``, so remote roles land in the same
``remote_india`` / ``remote_global`` buckets as ATS and HN postings. The internship test is the
shared ``InternshipMatcher``; stipend, duration and PPO come from ``stipend.py``.
"""

from collections.abc import Sequence
from datetime import datetime

from src.collectors.config import RemoteFeedsConfig
from src.enrichment.geo import GeoResolver
from src.enrichment.stipend import Stipend, extract_internship_terms
from src.models import CollectedOpportunity, Opportunity, ResolvedLocation, Startup

MAX_DESCRIPTION_CHARS = 4000
INDIA_REMOTE_QUERY = "Remote (India)"
WORLDWIDE_REMOTE_QUERY = "Remote - Worldwide"


class IndiaOpenness:
    """Decides whether a remote role is open to India and returns its resolved location."""

    def __init__(self, config: RemoteFeedsConfig, geo: GeoResolver) -> None:
        self.config = config
        self.geo = geo
        self._india = {n.strip().lower() for n in config.india_country_names}
        self._worldwide = [k.lower() for k in config.worldwide_keywords]

    def _located(self, query: str, raw: str) -> ResolvedLocation:
        return self.geo.resolve(query).model_copy(update={"raw_location": raw[:300] or None})

    def _worldwide_ok(self, raw: str) -> ResolvedLocation | None:
        if not self.config.keep_worldwide:
            return None
        return self._located(WORLDWIDE_REMOTE_QUERY, raw)

    def structured(
        self, countries: Sequence[str], utc_offsets: Sequence[float]
    ) -> ResolvedLocation | None:
        """Location for a role with country and timezone restrictions (Himalayas)."""
        names = [c.strip() for c in countries if c and c.strip()]
        raw = "Remote: " + (", ".join(names) or "Worldwide")
        if names:
            if any(n.lower() in self._india for n in names):
                return self._located(INDIA_REMOTE_QUERY, raw)
            return None
        offsets = set(utc_offsets)
        if offsets and self.config.india_utc_offset not in offsets:
            return None  # timezone-locked elsewhere
        if offsets == {self.config.india_utc_offset}:
            return self._located(INDIA_REMOTE_QUERY, f"{raw} (IST)")
        return self._worldwide_ok(raw)

    def free_text(self, location: str) -> ResolvedLocation | None:
        """Location for a role described only by text such as ``"Worldwide"`` (Remotive)."""
        text = " ".join(location.split())
        resolved = self.geo.resolve(f"{text} (Remote)" if text else "Remote")
        if resolved.is_india:
            return resolved.model_copy(update={"raw_location": text or None})
        lowered = text.lower()
        if any(keyword in lowered for keyword in self._worldwide):
            return self._worldwide_ok(text)
        return None


def remote_opportunity(
    *,
    source: str,
    title: str,
    url: str,
    description: str,
    location: ResolvedLocation,
    posted_at: datetime | None,
    stipend: Stipend | None = None,
    startup: Startup | None = None,
) -> CollectedOpportunity:
    """One internship. ``startup`` only when the source publishes the company's own domain."""
    terms = extract_internship_terms(f"{title}\n{description}")
    paid = stipend if stipend is not None and stipend.has_amount else terms.stipend
    opportunity = Opportunity(
        source=source,
        title=title[:300],
        url=url,
        description=description[:MAX_DESCRIPTION_CHARS] or None,
        stipend_inr_min=paid.min_inr,
        stipend_inr_max=paid.max_inr,
        is_paid=paid.is_paid,
        duration_months=terms.duration_months,
        has_ppo=terms.has_ppo,
        posted_at=posted_at,
    ).with_location(location)
    return CollectedOpportunity(startup=startup, opportunity=opportunity)
