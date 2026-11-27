"""``Startup`` — one company, deduplicated on ``canonical_domain``."""

import re
from datetime import date
from typing import Self

from pydantic import Field, field_validator, model_validator

from src.core.urls import canonical_domain
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
from src.models.enums import CITY_UNKNOWN, AtsProvider, Country, FundingStage
from src.models.location import ResolvedLocation

_GITHUB_ORG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class Startup(DomainModel):
    """A startup discovered from any source.

    Unknown facts are ``None`` (not ``False``/``0``) so repository upserts never overwrite known
    data with a guess. ``hq_city_canonical`` uses the ``"unknown"`` sentinel instead of ``None``.
    """

    id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    canonical_domain: str
    website_url: HttpUrlStr | None = None

    hq_country: Country | None = None
    hq_city_canonical: CitySlug = CITY_UNKNOWN
    hq_state: str | None = None
    hq_lat: Latitude | None = None
    hq_lng: Longitude | None = None
    hq_raw_location: str | None = None

    ats_provider: AtsProvider | None = None
    ats_slug: str | None = Field(default=None, min_length=1, max_length=200)

    funding_stage: FundingStage | None = None
    last_funded_at: date | None = None
    github_org: str | None = None
    team_size: int | None = Field(default=None, ge=1)
    tech_stack: Tags = Field(default_factory=list)

    hidden_score: int | None = Field(default=None, ge=0, le=100)
    open_to_cold_pitch: bool | None = None
    discovered_via: Slug | None = None
    last_checked_at: UtcDatetime | None = None

    @field_validator("canonical_domain", mode="before")
    @classmethod
    def _canonicalize(cls, value: object) -> object:
        return canonical_domain(value) if isinstance(value, str) else value

    @field_validator("github_org")
    @classmethod
    def _github_org(cls, value: str | None) -> str | None:
        if value is not None and not _GITHUB_ORG.match(value):
            raise ValueError(f"invalid GitHub org login: {value!r}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.hq_lat is None) != (self.hq_lng is None):
            raise ValueError("hq_lat and hq_lng must be set together")
        if self.ats_slug is not None and self.ats_provider is None:
            raise ValueError("ats_slug requires ats_provider")
        return self

    @classmethod
    def from_url(cls, name: str, url: str, **fields: object) -> Self:
        """Build a startup from any URL on its site (``canonical_domain`` derived from it)."""
        return cls.model_validate(
            {"name": name, "canonical_domain": url, "website_url": url, **fields}
        )

    def with_hq(self, location: ResolvedLocation) -> Self:
        """Return a copy with HQ fields taken from a resolved location."""
        return self.model_validate(
            self.model_dump()
            | {
                "hq_country": location.country,
                "hq_city_canonical": location.city_canonical,
                "hq_state": location.state,
                "hq_lat": location.lat,
                "hq_lng": location.lng,
                "hq_raw_location": location.raw_location,
            }
        )
