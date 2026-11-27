"""``StartupLead`` — a startup we heard about (funding news, incubator directory) but may not
have a website for yet. Deduplicated on a normalized company name (ADR 004)."""

import re
from datetime import date
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from src.core.text import normalize_text
from src.core.urls import canonical_domain
from src.models.base import (
    CitySlug,
    DomainModel,
    HttpUrlStr,
    Latitude,
    Longitude,
    Slug,
    UtcDatetime,
)
from src.models.enums import CITY_UNKNOWN, Country, FundingStage
from src.models.location import ResolvedLocation
from src.models.startup import Startup

# Trailing legal-entity words only. Brand words ("Labs", "Technologies") are kept on purpose:
# "Zeta" and "Zeta Labs" may be different companies.
_LEGAL_SUFFIX = re.compile(
    r"(?:\s+(?:private|pvt|limited|ltd|llp|inc|incorporated|opc|corp|corporation|co|gmbh|plc))+$"
)


def lead_name_key(name: str) -> str:
    """Dedup key: normalized name without trailing legal suffixes.

    ``"ExampleLabs Technologies Pvt. Ltd."`` -> ``"examplelabs technologies"``.
    """
    key = _LEGAL_SUFFIX.sub("", normalize_text(name)).strip()
    return key or normalize_text(name)


class StartupLead(DomainModel):
    """A company sighting from a non-ATS source. ``domain`` set => it can become a ``Startup``."""

    id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    domain: str | None = None
    source: Slug
    source_url: HttpUrlStr | None = None

    hq_country: Country | None = None
    hq_city_canonical: CitySlug = CITY_UNKNOWN
    hq_state: str | None = None
    hq_lat: Latitude | None = None
    hq_lng: Longitude | None = None
    hq_raw_location: str | None = None

    sector: str | None = Field(default=None, max_length=200)
    founders: list[str] = Field(default_factory=list)
    funding_stage: FundingStage | None = None
    funding_amount: str | None = Field(default=None, max_length=60)
    last_funded_at: date | None = None
    # Directory facts (YC India, ADR 008). ``batch`` is the accelerator cohort ("w2021");
    # ``is_hiring`` is True only when the directory itself lists the company as hiring.
    batch: str | None = Field(default=None, max_length=40)
    is_hiring: bool | None = None
    team_size: int | None = Field(default=None, ge=1)
    github_org: str | None = Field(default=None, max_length=100)

    startup_id: int | None = None
    first_seen_at: UtcDatetime | None = None
    last_seen_at: UtcDatetime | None = None

    @field_validator("domain", mode="before")
    @classmethod
    def _domain(cls, value: Any) -> Any:
        if isinstance(value, str):
            return canonical_domain(value) if value.strip() else None
        return value

    @field_validator("founders", mode="before")
    @classmethod
    def _founders(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, list):
            seen: dict[str, None] = {}
            for item in value:
                name = " ".join(str(item).split())
                if name:
                    seen.setdefault(name, None)
            return list(seen)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.hq_lat is None) != (self.hq_lng is None):
            raise ValueError("hq_lat and hq_lng must be set together")
        return self

    @property
    def name_key(self) -> str:
        return lead_name_key(self.name)

    def with_hq(self, location: ResolvedLocation) -> Self:
        """Copy with HQ fields from a resolved location (remote/unknown keep only country)."""
        return self.model_validate(
            self.model_dump()
            | {
                "hq_country": location.country,
                "hq_city_canonical": location.city_canonical if location.lat is not None else None,
                "hq_state": location.state,
                "hq_lat": location.lat,
                "hq_lng": location.lng,
                "hq_raw_location": location.raw_location,
            }
        )

    def to_startup(self) -> Startup | None:
        """A ``Startup`` carrying this lead's facts, or ``None`` without a domain."""
        if self.domain is None:
            return None
        return Startup(
            name=self.name,
            canonical_domain=self.domain,
            website_url=f"https://{self.domain}",
            hq_country=self.hq_country,
            hq_city_canonical=self.hq_city_canonical,
            hq_state=self.hq_state,
            hq_lat=self.hq_lat,
            hq_lng=self.hq_lng,
            hq_raw_location=self.hq_raw_location,
            funding_stage=self.funding_stage,
            last_funded_at=self.last_funded_at,
            team_size=self.team_size,
            github_org=self.github_org,
            discovered_via=self.source,
        )
