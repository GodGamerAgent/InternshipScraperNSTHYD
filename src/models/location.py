"""Output contract of the geo resolver (``src/enrichment/geo.py``)."""

from pydantic import ConfigDict, Field, model_validator

from src.models.base import CitySlug, DomainModel, Latitude, Longitude
from src.models.enums import (
    CITY_REMOTE_INDIA,
    CITY_UNKNOWN,
    Country,
    ResolutionMethod,
    WorkMode,
)


class ResolvedLocation(DomainModel):
    """Canonical location fields shared by ``Startup`` HQs and ``Opportunity`` postings.

    Attributes:
        raw_location: The original scraped string, kept for debugging.
        country: ``IN``, ``GLOBAL`` or ``None`` when nothing could be inferred.
        city_canonical: Primary city slug, ``"remote_india"`` or ``"unknown"``.
        all_cities: Every city mentioned, in order (multi-city posts such as "Bengaluru / Pune").
        work_mode: Inferred work mode; ``None`` when unknowable.
        resolution_method: Which pipeline step decided the city.
        matched_term: The config term that matched (e.g. ``"Gachibowli"``).
    """

    model_config = ConfigDict(frozen=True)

    raw_location: str | None = None
    country: Country | None = None
    city_canonical: CitySlug = CITY_UNKNOWN
    state: str | None = None
    work_mode: WorkMode | None = None
    lat: Latitude | None = None
    lng: Longitude | None = None
    all_cities: list[str] = Field(default_factory=list)
    resolution_method: ResolutionMethod = ResolutionMethod.UNRESOLVED
    matched_term: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ResolvedLocation":
        if (self.lat is None) != (self.lng is None):
            raise ValueError("lat and lng must be set together")
        if self.city_canonical == CITY_REMOTE_INDIA and self.work_mode is not WorkMode.REMOTE_INDIA:
            raise ValueError("city_canonical 'remote_india' requires work_mode 'remote_india'")
        return self

    @property
    def is_india(self) -> bool:
        """True for any India-based or Remote-India location."""
        return self.country is Country.IN

    @property
    def is_resolved(self) -> bool:
        """True when a concrete city (or Remote-India) was determined."""
        return self.city_canonical != CITY_UNKNOWN
