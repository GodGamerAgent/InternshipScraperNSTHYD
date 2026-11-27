"""India geolocation resolver: messy location strings -> :class:`ResolvedLocation`.

Three-step pipeline (``implementation.md`` §2, ``docs/ARCHITECTURE.md`` §3):

1. **Alias & neighborhood match** against ``config/india_geo.yaml`` (whole-word, longest-first,
   punctuation-insensitive). Weak ``state_aliases`` / state-only names are used only if no city hit.
2. **Work-mode / Remote-India detection.** Explicit Remote-India phrases, or a remote token plus any
   India signal, give ``remote_india``; a remote token with no India signal gives ``remote_global``.
3. **HQ fallback** when no city is known: PIN code in the string -> PIN code in ``hq_text``
   (careers/contact/footer text) -> the startup's known ``hq_city`` -> the ``source_default_city``.

All rules live in YAML; this module contains only the algorithm.
"""

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.config import load_yaml
from src.core.text import normalize_text
from src.models import (
    CITY_REMOTE_INDIA,
    CITY_UNKNOWN,
    Country,
    ResolutionMethod,
    ResolvedLocation,
    WorkMode,
)
from src.models.enums import RESERVED_CITY_SLUGS

EARTH_RADIUS_KM = 6371.0088
# Indian PIN: 6 digits, first digit 1-8, optional space after the 3-digit sorting district.
_PIN = re.compile(r"(?<!\d)([1-8]\d{2}) ?(\d{3})(?!\d)")
_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------- config schema
class CityConfig(BaseModel):
    """One city block in ``india_geo.yaml``."""

    model_config = ConfigDict(extra="forbid")

    display_name: str
    state: str
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    aliases: list[str] = Field(min_length=1)
    neighborhoods: list[str] = Field(default_factory=list)
    state_aliases: list[str] = Field(default_factory=list)
    state_overrides: dict[str, list[str]] = Field(default_factory=dict)
    pin_prefixes: list[str] = Field(default_factory=list)

    @field_validator("pin_prefixes")
    @classmethod
    def _pin_prefixes(cls, prefixes: list[str]) -> list[str]:
        for prefix in prefixes:
            if not re.fullmatch(r"[1-8]\d{0,5}", prefix):
                raise ValueError(f"PIN prefix must be 1-6 digits starting 1-8, got {prefix!r}")
        return prefixes

    def terms(self) -> list[str]:
        """Every term that identifies this city (aliases, neighborhoods, state aliases)."""
        return [*self.aliases, *self.neighborhoods, *self.state_aliases]


class GeoConfig(BaseModel):
    """Validated contents of ``config/india_geo.yaml``.

    Validation rejects ambiguous configs: a term mapped to two cities (or to a city and a
    state-only entry), a PIN prefix owned by two cities, reserved/invalid slugs, and
    ``state_overrides`` terms that are not aliases/neighborhoods of that city.
    """

    model_config = ConfigDict(extra="forbid")

    cities: dict[str, CityConfig] = Field(min_length=1)
    states: dict[str, list[str]] = Field(default_factory=dict)
    ignore_phrases: list[str] = Field(default_factory=list)
    india_markers: list[str] = Field(default_factory=list)
    remote_tokens: list[str] = Field(default_factory=list)
    remote_india_phrases: list[str] = Field(default_factory=list)
    hybrid_phrases: list[str] = Field(default_factory=list)
    onsite_phrases: list[str] = Field(default_factory=list)
    foreign_markers: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unambiguous(self) -> Self:
        owner: dict[str, str] = {}
        pin_owner: dict[str, str] = {}
        for slug, city in self.cities.items():
            if not _SLUG.match(slug) or slug in RESERVED_CITY_SLUGS:
                raise ValueError(f"invalid or reserved city slug: {slug!r}")
            for term in city.terms():
                key = normalize_text(term)
                if not key:
                    raise ValueError(f"empty term in city {slug!r}: {term!r}")
                previous = owner.setdefault(key, slug)
                if previous != slug:
                    raise ValueError(f"term {term!r} maps to both {previous!r} and {slug!r}")
            own_terms = {normalize_text(t) for t in (*city.aliases, *city.neighborhoods)}
            for state, terms in city.state_overrides.items():
                for term in terms:
                    if normalize_text(term) not in own_terms:
                        raise ValueError(
                            f"state_overrides[{state!r}] term {term!r} is not an alias or "
                            f"neighborhood of {slug!r}"
                        )
            for prefix in city.pin_prefixes:
                previous = pin_owner.setdefault(prefix, slug)
                if previous != slug:
                    raise ValueError(f"PIN prefix {prefix!r} maps to {previous!r} and {slug!r}")
        for state, terms in self.states.items():
            for term in terms:
                if normalize_text(term) in owner:
                    raise ValueError(
                        f"state term {term!r} ({state}) collides with city "
                        f"{owner[normalize_text(term)]!r}"
                    )
        return self

    @classmethod
    def load(cls, config_dir: Path | None = None) -> Self:
        """Load and validate ``<config_dir>/india_geo.yaml``."""
        return cls.model_validate(load_yaml("india_geo", config_dir))


# ---------------------------------------------------------------------------- matching
class _TermMatcher:
    """Whole-word, longest-first matcher over :func:`normalize_text` output."""

    def __init__(self, terms: Iterable[str]) -> None:
        keys = sorted({k for k in map(normalize_text, terms) if k}, key=len, reverse=True)
        self._regex = (
            re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, keys)) + r")(?!\w)")
            if keys
            else None
        )

    def find_all(self, text: str) -> list[str]:
        """All non-overlapping matches, left to right."""
        return [m.group() for m in self._regex.finditer(text)] if self._regex else []

    def first(self, text: str) -> str | None:
        """Leftmost match or ``None``."""
        match = self._regex.search(text) if self._regex else None
        return match.group() if match else None

    def remove(self, text: str) -> str:
        """``text`` with every match replaced by a space."""
        return self._regex.sub(" ", text) if self._regex else text


@dataclass(frozen=True, slots=True)
class _CityTerm:
    slug: str
    method: ResolutionMethod
    term: str  # as written in config, for ResolvedLocation.matched_term
    state: str  # city state, or the state_overrides entry for this term


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------- resolver
class GeoResolver:
    """Deterministic India location resolver. Build once and reuse (matchers are precompiled)."""

    def __init__(self, config: GeoConfig) -> None:
        self.config = config
        self._city_terms: dict[str, _CityTerm] = {}
        self._state_alias_terms: dict[str, _CityTerm] = {}
        self._pins: dict[str, str] = {}
        for slug, city in config.cities.items():
            overrides = {
                normalize_text(term): state
                for state, terms in city.state_overrides.items()
                for term in terms
            }
            for method, terms in (
                (ResolutionMethod.ALIAS, city.aliases),
                (ResolutionMethod.NEIGHBORHOOD, city.neighborhoods),
            ):
                for term in terms:
                    key = normalize_text(term)
                    self._city_terms[key] = _CityTerm(
                        slug, method, term, overrides.get(key, city.state)
                    )
            for term in city.state_aliases:
                self._state_alias_terms[normalize_text(term)] = _CityTerm(
                    slug, ResolutionMethod.STATE_ALIAS, term, city.state
                )
            self._pins.update(dict.fromkeys(city.pin_prefixes, slug))
        self._pin_lengths = sorted({len(p) for p in self._pins}, reverse=True)
        self._states = {
            normalize_text(term): state for state, terms in config.states.items() for term in terms
        }

        self._city_matcher = _TermMatcher(self._city_terms)
        self._state_alias_matcher = _TermMatcher(self._state_alias_terms)
        self._state_matcher = _TermMatcher(self._states)
        self._ignore = _TermMatcher(config.ignore_phrases)
        self._india = _TermMatcher(config.india_markers)
        self._remote = _TermMatcher(config.remote_tokens)
        self._remote_india = _TermMatcher(config.remote_india_phrases)
        self._hybrid = _TermMatcher(config.hybrid_phrases)
        self._onsite = _TermMatcher(config.onsite_phrases)
        self._foreign = _TermMatcher(config.foreign_markers)

    @classmethod
    def from_config(cls, config_dir: Path | None = None) -> Self:
        """Build a resolver from ``<config_dir>/india_geo.yaml`` (default: env config dir)."""
        return cls(GeoConfig.load(config_dir))

    # -------------------------------------------------------------- lookups
    @property
    def city_slugs(self) -> list[str]:
        """Configured canonical city slugs, in config order."""
        return list(self.config.cities)

    def city(self, slug: str) -> CityConfig:
        """Config for ``slug``. Raises ``KeyError`` listing valid slugs if unknown."""
        try:
            return self.config.cities[slug]
        except KeyError:
            known = ", ".join(self.config.cities)
            raise KeyError(f"unknown city {slug!r}; known: {known}") from None

    def city_for_pin(self, pin: str) -> str | None:
        """City slug for a 6-digit PIN code via longest configured prefix, else ``None``."""
        digits = re.sub(r"\D", "", pin)
        if len(digits) != 6:
            return None
        for length in self._pin_lengths:
            if slug := self._pins.get(digits[:length]):
                return slug
        return None

    def find_pin_city(self, text: str | None) -> tuple[str, str] | None:
        """First PIN code in ``text`` whose prefix maps to a city -> ``(slug, pin)``."""
        for match in _PIN.finditer(text or ""):
            pin = match.group(1) + match.group(2)
            if slug := self.city_for_pin(pin):
                return slug, pin
        return None

    def cities_within(self, lat: float, lng: float, radius_km: float) -> list[str]:
        """City slugs whose centroid is within ``radius_km`` of a point, nearest first."""
        distances = (
            (haversine_km(lat, lng, c.lat, c.lng), slug) for slug, c in self.config.cities.items()
        )
        return [slug for dist, slug in sorted(distances) if dist <= radius_km]

    # -------------------------------------------------------------- main entry point
    def resolve(
        self,
        raw_location: str | None,
        *,
        hq_text: str | None = None,
        hq_city: str | None = None,
        source_default_city: str | None = None,
    ) -> ResolvedLocation:
        """Resolve a raw location string into canonical fields.

        Args:
            raw_location: Location string from the listing (``"Gachibowli, Hyderabad"``).
            hq_text: Optional careers/contact/footer text searched for PIN codes (step 3).
            hq_city: The startup's already-known HQ slug, used when the listing has no city. Also
                marks the listing as coming from an Indian company, so a bare ``"Remote"`` is
                treated as ``remote_india`` rather than ``remote_global``.
            source_default_city: City implied by the source (T-Hub -> ``"hyderabad"``); last resort.

        Raises:
            ValueError: If ``hq_city`` / ``source_default_city`` is not a configured slug.
        """
        for name, slug in (("hq_city", hq_city), ("source_default_city", source_default_city)):
            if slug is not None and slug not in self.config.cities:
                raise ValueError(f"{name}={slug!r} is not a configured city")

        raw = raw_location.strip() if raw_location and raw_location.strip() else None
        text = normalize_text(raw)
        city_text = self._ignore.remove(text)

        # Step 1 — alias / neighborhood, then weak state-level signals.
        hits = [self._city_terms[key] for key in self._city_matcher.find_all(city_text)]
        if not hits and (key := self._state_alias_matcher.first(city_text)):
            hits = [self._state_alias_terms[key]]
        state_only = None if hits else self._states.get(self._state_matcher.first(city_text) or "")
        india_signal = bool(hits) or state_only is not None or self._india.first(text) is not None
        foreign = self._foreign.first(text) is not None
        india_context = india_signal or (
            not foreign and (hq_city is not None or source_default_city is not None)
        )

        # Step 2 — work mode.
        work_mode, remote_term = self._work_mode(text, india_context, has_city=bool(hits))

        if hits:
            primary = hits[0]
            return self._city_location(
                raw,
                primary.slug,
                state=primary.state,
                work_mode=work_mode,
                all_cities=list(dict.fromkeys(h.slug for h in hits)),
                method=primary.method,
                term=primary.term,
            )
        if work_mode is WorkMode.REMOTE_INDIA:
            return ResolvedLocation(
                raw_location=raw,
                country=Country.IN,
                city_canonical=CITY_REMOTE_INDIA,
                state=state_only,
                work_mode=work_mode,
                resolution_method=ResolutionMethod.REMOTE,
                matched_term=remote_term,
            )
        if work_mode is WorkMode.REMOTE_GLOBAL or (foreign and not india_signal):
            return ResolvedLocation(
                raw_location=raw,
                country=Country.GLOBAL,
                work_mode=work_mode,
                resolution_method=(
                    ResolutionMethod.REMOTE
                    if work_mode is WorkMode.REMOTE_GLOBAL
                    else ResolutionMethod.FOREIGN
                ),
                matched_term=remote_term,
            )

        # Step 3 — HQ fallback (PIN in string -> PIN in hq_text -> hq_city -> source default).
        if fallback := self._hq_fallback(text, hq_text, hq_city, source_default_city):
            slug, method, term = fallback
            return self._city_location(
                raw,
                slug,
                state=self.config.cities[slug].state,
                work_mode=work_mode,
                all_cities=[slug],
                method=method,
                term=term,
            )
        return ResolvedLocation(
            raw_location=raw,
            country=Country.IN if india_signal else None,
            city_canonical=CITY_UNKNOWN,
            state=state_only,
            work_mode=work_mode,
            resolution_method=(
                ResolutionMethod.STATE if state_only else ResolutionMethod.UNRESOLVED
            ),
        )

    # -------------------------------------------------------------- internals
    def _work_mode(
        self, text: str, india_context: bool, *, has_city: bool
    ) -> tuple[WorkMode | None, str | None]:
        if term := self._hybrid.first(text):
            return WorkMode.HYBRID, term
        if term := self._remote_india.first(text):
            return WorkMode.REMOTE_INDIA, term
        if term := self._remote.first(text):
            return (WorkMode.REMOTE_INDIA if india_context else WorkMode.REMOTE_GLOBAL), term
        if has_city or self._onsite.first(text):
            return WorkMode.ONSITE, None
        return None, None

    def _hq_fallback(
        self,
        text: str,
        hq_text: str | None,
        hq_city: str | None,
        source_default_city: str | None,
    ) -> tuple[str, ResolutionMethod, str | None] | None:
        if found := self.find_pin_city(text):
            return found[0], ResolutionMethod.PIN_CODE, found[1]
        if hq_text and (found := self.find_pin_city(normalize_text(hq_text))):
            return found[0], ResolutionMethod.HQ_TEXT_PIN, found[1]
        if hq_city:
            return hq_city, ResolutionMethod.HQ_CITY, None
        if source_default_city:
            return source_default_city, ResolutionMethod.SOURCE_DEFAULT, None
        return None

    def _city_location(
        self,
        raw: str | None,
        slug: str,
        *,
        state: str,
        work_mode: WorkMode | None,
        all_cities: list[str],
        method: ResolutionMethod,
        term: str | None,
    ) -> ResolvedLocation:
        city = self.config.cities[slug]
        return ResolvedLocation(
            raw_location=raw,
            country=Country.IN,
            city_canonical=slug,
            state=state,
            work_mode=work_mode,
            lat=city.lat,
            lng=city.lng,
            all_cities=all_cities,
            resolution_method=method,
            matched_term=term,
        )


@lru_cache(maxsize=1)
def default_resolver() -> GeoResolver:
    """Process-wide resolver built from the default config dir (cached)."""
    return GeoResolver.from_config()
