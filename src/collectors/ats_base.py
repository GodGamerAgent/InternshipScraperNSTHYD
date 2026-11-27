"""Shared machinery for Tier 1 public ATS collectors (Greenhouse, Lever, Ashby, Workable).

Each provider module only knows its JSON shape: it implements ``postings(raw)`` (list the job
dicts) and ``to_job(posting)`` (map one dict to a provider-neutral :class:`AtsJob`). Everything
India-specific lives here, once:

1. internship filter (``InternshipMatcher``, title + employment type),
2. location resolution through ``GeoResolver`` — the ATS workplace hint ("remote"/"hybrid") and
   country code are folded into the location string, and the seed's ``hq_city`` is used only when
   the posting says nothing more specific than "India" / "Remote" / nothing,
3. India filter (drop postings that are not in India or Remote-India),
4. stipend / duration / PPO extraction (structured compensation wins over text),
5. one ``CollectedOpportunity`` per posting, with the ``Startup`` built from the seed.

A malformed posting is logged and skipped; it never drops the rest of the board.
"""

import logging
from abc import abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from urllib.parse import quote

from pydantic import ValidationError

from src.collectors.config import AtsSourceConfig, CompanySeed
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.enrichment.stipend import Stipend, extract_internship_terms
from src.models import (
    AtsProvider,
    CollectedOpportunity,
    Country,
    Opportunity,
    ResolvedLocation,
    Startup,
    WorkMode,
)

logger = logging.getLogger(__name__)

Workplace = Literal["onsite", "hybrid", "remote"]
MAX_DESCRIPTION_CHARS = 4000


@dataclass(frozen=True, slots=True)
class AtsJob:
    """Provider-neutral view of one posting (output of ``to_job``)."""

    title: str
    url: str
    locations: tuple[str, ...] = ()
    employment_type: str | None = None
    workplace: Workplace | None = None
    country_code: str | None = None  # ISO-2 if the ATS provides one ("IN", "US", …)
    description: str = ""
    posted_at: datetime | None = None
    stipend: Stipend | None = None  # structured compensation, if the ATS exposes it


class AtsCollector(BaseCollector[CompanySeed, Any, CollectedOpportunity]):
    """Base class for one ATS provider. Subclasses set ``name``/``provider`` and the two hooks."""

    provider: ClassVar[AtsProvider]
    # Tier 1 APIs opt out of robots.txt (ADR 002). Providers added later respect it (ADR 009).
    respect_robots: ClassVar[bool] = False

    def __init__(
        self,
        http: HttpClient,
        *,
        source: AtsSourceConfig,
        geo: GeoResolver,
        matcher: InternshipMatcher,
        india_only: bool = True,
    ) -> None:
        super().__init__(http)
        self.source = source
        self.geo = geo
        self.matcher = matcher
        self.india_only = india_only

    # -------------------------------------------------------------- provider hooks
    @abstractmethod
    def postings(self, raw: Any) -> list[Mapping[str, Any]]:
        """The list of job dicts inside ``raw``. Raise ``ValueError`` on an unexpected shape."""

    @abstractmethod
    def to_job(self, posting: Mapping[str, Any]) -> AtsJob:
        """Map one provider job dict to an :class:`AtsJob` (may raise on malformed data)."""

    # -------------------------------------------------------------- BaseCollector hooks
    def targets(self) -> list[CompanySeed]:
        return list(self.source.companies)

    def describe_target(self, target: CompanySeed) -> str:
        return target.slug

    def close_scope(self, target: CompanySeed) -> str | None:
        return target.domain  # one company's board

    async def fetch(self, target: CompanySeed) -> Any:
        url = self.source.endpoint.format(slug=quote(target.slug, safe=""))
        # Documented public job-board API. Tier 1 opts out of robots.txt (ADR 002); newer providers
        # keep it on (ADR 009). Rate limiting, retries and caching apply either way.
        return await self.http.get_json(url, respect_robots=self.respect_robots)

    def parse(self, raw: Any, target: CompanySeed) -> Iterable[CollectedOpportunity]:
        startup = self.build_startup(target)
        items: list[CollectedOpportunity] = []
        skipped = filtered = 0
        for posting in self.postings(raw):
            try:
                job = self.to_job(posting)
                if not self.matcher.is_internship(job.title, employment_type=job.employment_type):
                    filtered += 1
                    continue
                location = self.resolve_location(job, target)
                if self.india_only and not location.is_india:
                    filtered += 1
                    continue
                items.append(
                    CollectedOpportunity(
                        startup=startup, opportunity=self.build_opportunity(job, location)
                    )
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                skipped += 1
                logger.warning(
                    "collector.posting_skipped",
                    extra={
                        "collector": self.name,
                        "target": target.slug,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300],
                    },
                )
        logger.debug(
            "collector.board_parsed",
            extra={
                "collector": self.name,
                "target": target.slug,
                "kept": len(items),
                "filtered": filtered,
                "skipped": skipped,
            },
        )
        return items

    # -------------------------------------------------------------- shared logic
    def build_startup(self, seed: CompanySeed) -> Startup:
        """``Startup`` for a seed (HQ fields from ``india_geo.yaml`` when ``hq_city`` is set)."""
        fields: dict[str, Any] = {
            "name": seed.name,
            "canonical_domain": seed.domain,
            "website_url": f"https://{seed.domain}",
            "ats_provider": self.provider,
            "ats_slug": seed.slug,
            "discovered_via": self.name,
        }
        if seed.hq_city:
            city = self.geo.city(seed.hq_city)
            fields |= {
                "hq_country": Country.IN,
                "hq_city_canonical": seed.hq_city,
                "hq_state": city.state,
                "hq_lat": city.lat,
                "hq_lng": city.lng,
                "hq_raw_location": city.display_name,
            }
        return Startup.model_validate(fields)

    def resolve_location(self, job: AtsJob, seed: CompanySeed) -> ResolvedLocation:
        """Resolve a posting's location, using the seed HQ only for generic locations."""
        raw = " / ".join(dict.fromkeys(loc.strip() for loc in job.locations if loc and loc.strip()))
        query = raw
        if job.workplace == "remote":
            query += " (Remote)"
        elif job.workplace == "hybrid":
            query += " (Hybrid)"
        if (job.country_code or "").upper() == "IN":
            query += " India"

        first = self.geo.resolve(query)
        generic = (
            not raw
            or (first.country is Country.IN and not first.is_resolved)
            or first.work_mode is WorkMode.REMOTE_GLOBAL
        )
        foreign_code = bool(job.country_code) and job.country_code.upper() != "IN"
        location = (
            self.geo.resolve(query, hq_city=seed.hq_city)
            if seed.hq_city and generic and not foreign_code
            else first
        )
        return location.model_copy(update={"raw_location": raw or None})

    def build_opportunity(self, job: AtsJob, location: ResolvedLocation) -> Opportunity:
        """Validated ``Opportunity`` with India stipend/duration/PPO fields filled in."""
        terms = extract_internship_terms(f"{job.title}\n{job.description}")
        stipend = job.stipend if job.stipend and job.stipend.has_amount else terms.stipend
        description = job.description[:MAX_DESCRIPTION_CHARS] or None
        return Opportunity(
            source=self.name,
            title=job.title,
            url=job.url,
            description=description,
            stipend_inr_min=stipend.min_inr,
            stipend_inr_max=stipend.max_inr,
            is_paid=stipend.is_paid,
            duration_months=terms.duration_months,
            has_ppo=terms.has_ppo,
            posted_at=job.posted_at,
        ).with_location(location)


def require_list(value: Any, what: str) -> list[Mapping[str, Any]]:
    """``value`` if it is a list of dicts, else ``ValueError`` (unexpected API shape)."""
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"expected {what} to be a list of objects, got {type(value).__name__}")
    return value


def clean(value: Any) -> str | None:
    """Stripped string or ``None`` for empty / non-string values."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def parse_timestamp(value: Any) -> datetime | None:
    """ISO-8601 string, ``YYYY-MM-DD`` date, or epoch milliseconds -> aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
