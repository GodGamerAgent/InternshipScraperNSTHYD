"""``Opportunity`` — an internship posting or a hidden lead, deduplicated by fingerprint."""

import hashlib
from typing import Self

from pydantic import Field, model_validator

from src.core.text import normalize_text
from src.core.urls import normalize_url
from src.models.base import (
    CitySlug,
    DomainModel,
    HttpUrlStr,
    Latitude,
    Longitude,
    Slug,
    Tags,
    UtcDatetime,
)
from src.models.enums import CITY_REMOTE_INDIA, CITY_UNKNOWN, Country, OpportunityStatus, WorkMode
from src.models.location import ResolvedLocation


def opportunity_fingerprint(canonical_domain: str, title: str, url: str) -> str:
    """Deterministic dedup key: sha256 of ``domain | normalized title | normalized URL``.

    Stable across whitespace/case/punctuation changes in the title and tracking-param or
    trailing-slash changes in the URL.
    """
    key = "|".join((canonical_domain.lower(), normalize_text(title), normalize_url(url)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Opportunity(DomainModel):
    """An internship listing (from an ATS, HN, …) or an inferred hidden lead.

    Stipends are monthly INR. ``is_paid`` is inferred as ``True`` when a positive stipend is given.
    """

    id: int | None = None
    startup_id: int | None = None
    source: Slug
    title: str = Field(min_length=1, max_length=300)
    url: HttpUrlStr
    description: str | None = None

    raw_location: str | None = None
    country: Country | None = None
    city_canonical: CitySlug = CITY_UNKNOWN
    state: str | None = None
    work_mode: WorkMode | None = None
    lat: Latitude | None = None
    lng: Longitude | None = None

    stipend_inr_min: int | None = Field(default=None, ge=0)
    stipend_inr_max: int | None = Field(default=None, ge=0)
    is_paid: bool | None = None
    duration_months: float | None = Field(default=None, gt=0, le=36)
    has_ppo: bool | None = None

    is_hidden_lead: bool = False
    tech_stack: Tags = Field(default_factory=list)
    posted_at: UtcDatetime | None = None
    status: OpportunityStatus = OpportunityStatus.OPEN

    # Managed by the repository; ignored on upsert.
    first_seen_at: UtcDatetime | None = None
    last_seen_at: UtcDatetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _infer_paid(cls, data: object) -> object:
        if (
            isinstance(data, dict)
            and data.get("is_paid") is None
            and any(data.get(k) for k in ("stipend_inr_min", "stipend_inr_max"))
        ):
            return data | {"is_paid": True}
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        lo, hi = self.stipend_inr_min, self.stipend_inr_max
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"stipend_inr_min ({lo}) > stipend_inr_max ({hi})")
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be set together")
        if self.city_canonical == CITY_REMOTE_INDIA and self.work_mode is not WorkMode.REMOTE_INDIA:
            raise ValueError("city_canonical 'remote_india' requires work_mode 'remote_india'")
        if self.is_paid is False and (lo or hi):
            raise ValueError("is_paid=False contradicts a positive stipend")
        return self

    def fingerprint(self, canonical_domain: str) -> str:
        """Dedup key for this posting under the owning startup's ``canonical_domain``."""
        return opportunity_fingerprint(canonical_domain, self.title, self.url)

    def with_location(self, location: ResolvedLocation) -> Self:
        """Return a copy with location fields taken from a resolved location."""
        return self.model_validate(
            self.model_dump()
            | {
                "raw_location": location.raw_location,
                "country": location.country,
                "city_canonical": location.city_canonical,
                "state": location.state,
                "work_mode": location.work_mode,
                "lat": location.lat,
                "lng": location.lng,
            }
        )
