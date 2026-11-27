"""Validated schema for ``config/sources.yaml``.

Every section is modelled and strict (unknown keys are errors), so a typo in YAML fails at load
time instead of silently disabling a rule. Enrichment modules receive these models (or plain
values from them); they never import this module (layering rule in ``AGENTS.md``).
"""

import re
from datetime import date
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.config import load_yaml
from src.core.urls import canonical_domain
from src.models import AtsProvider

ATS_PROVIDERS = (
    AtsProvider.GREENHOUSE,
    AtsProvider.LEVER,
    AtsProvider.ASHBY,
    AtsProvider.WORKABLE,
)
# Public JSON job-board APIs (Tier 1 + ADR 009). ``ats_probe`` / ``ats_search`` may use these: they
# verify boards through JSON. The ADR 009 providers are fetched with robots.txt respected.
JSON_ATS_PROVIDERS = (
    *ATS_PROVIDERS,
    AtsProvider.RIPPLING,
    AtsProvider.BREEZYHR,
    AtsProvider.RECRUITEE,
)
# Providers with a polling collector (JSON APIs + India-popular HTML portals, ADR 008).
POLLED_ATS_PROVIDERS = (*JSON_ATS_PROVIDERS, AtsProvider.ZOHORECRUIT)
# A Zoho Recruit seed's slug is the full portal host; anything else would let a slug point the
# collector at an arbitrary host.
ZOHO_PORTAL_HOST = re.compile(r"[a-z0-9][a-z0-9-]*\.zohorecruit\.(?:in|com)")


def _compile_all(patterns: list[str]) -> list[str]:
    for pattern in patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
    return patterns


class CompanySeed(BaseModel):
    """One company polled on a public ATS board."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    name: str = Field(min_length=1)
    slug: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    domain: str
    hq_city: str | None = None
    verified_on: date | None = None
    notes: str | None = None

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return canonical_domain(value)


class AtsSourceConfig(BaseModel):
    """Endpoint template + seed companies for one ATS provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    companies: list[CompanySeed] = Field(default_factory=list)

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        if "{slug}" not in value or not value.startswith("https://"):
            raise ValueError("endpoint must be an https URL containing '{slug}'")
        return value

    @model_validator(mode="after")
    def _unique(self) -> Self:
        slugs = [c.slug.lower() for c in self.companies]
        duplicates = {s for s in slugs if slugs.count(s) > 1}
        if duplicates:
            raise ValueError(f"duplicate company slugs: {sorted(duplicates)}")
        return self


class InternshipConfig(BaseModel):
    """Title / employment-type rules deciding whether a posting is an internship."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    include_patterns: list[str] = Field(min_length=1)
    exclude_title_patterns: list[str] = Field(default_factory=list)
    employment_type_patterns: list[str] = Field(default_factory=list)

    _check = field_validator(
        "include_patterns", "exclude_title_patterns", "employment_type_patterns"
    )(_compile_all)


class FeedConfig(BaseModel):
    """One funding-news RSS feed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    url: str = Field(pattern=r"^https://")


IncubatorKind = Literal["tablepress", "webflow", "iitm_api", "nsrcel_popup"]


class IncubatorConfig(BaseModel):
    """One incubator directory: an HTML page or (``iitm_api``) a paginated public JSON API.

    ``kind`` selects the parser and the collector: ``tablepress`` / ``webflow`` → ``thub``,
    ``iitm_api`` → ``iitm``, ``nsrcel_popup`` → ``nsrcel``. ``page_size`` / ``max_pages`` only
    apply to paginated kinds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: IncubatorKind
    url: str = Field(pattern=r"^https://")
    default_city: str | None = None
    page_size: int = Field(default=100, ge=1, le=200)
    max_pages: int = Field(default=10, ge=1, le=50)


class YcDirectoryConfig(BaseModel):
    """``yc_india``: Y Combinator's public company directory, India location (ADR 008).

    ``listing_url`` / ``hiring_url`` are server-rendered pages (robots.txt allows them; the
    ``api.ycombinator.com`` API is robots-disallowed and never used). A company counts as hiring
    when it appears on the ``hiring_url`` listing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    listing_url: str = Field(pattern=r"^https://www\.ycombinator\.com/companies/")
    hiring_url: str | None = Field(
        default=None, pattern=r"^https://www\.ycombinator\.com/companies/"
    )
    max_pages: int = Field(default=10, ge=1, le=50)
    statuses: list[str] = Field(default_factory=lambda: ["Active"], min_length=1)


class HackerNewsConfig(BaseModel):
    """HN "Who is hiring?" thread filtering rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    search_url: str
    comments_url: str
    who_is_hiring_author: str
    story_title_pattern: str
    max_comments: int = Field(default=1000, ge=1, le=1000)
    remote_global_keywords: list[str] = Field(default_factory=list)
    role_keywords: list[str] = Field(min_length=1)
    seeker_patterns: list[str] = Field(default_factory=list)

    _check = field_validator("role_keywords", "seeker_patterns")(_compile_all)


class SiteConfig(BaseModel):
    """How a startup's own website is probed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    careers_paths: list[str] = Field(min_length=1)
    max_pages: int = Field(default=8, ge=1, le=20)
    spa_min_text_chars: int = Field(default=400, ge=0)

    @field_validator("careers_paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        if not all(p.startswith("/") for p in value):
            raise ValueError("careers_paths must start with '/'")
        return value


class ContactsConfig(BaseModel):
    """Public-contact discovery rules (see ``src/enrichment/contacts.py``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hiring_mailbox_prefixes: list[str] = Field(min_length=1)
    excluded_mailbox_prefixes: list[str] = Field(default_factory=list)
    profile_hosts: list[str] = Field(default_factory=list)
    pattern_templates: list[str] = Field(default_factory=list)
    max_github_members: int = Field(default=5, ge=0, le=30)

    @field_validator("pattern_templates")
    @classmethod
    def _templates(cls, value: list[str]) -> list[str]:
        for template in value:
            if "@" in template or "{first}" not in template:
                raise ValueError(f"pattern template must use {{first}} and no '@': {template!r}")
        return value


class AtsProbeConfig(BaseModel):
    """``ats_probe``: slug guessing + verification during ``enrich`` (ADR 007)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    providers: list[AtsProvider] = Field(default_factory=lambda: list(ATS_PROVIDERS))
    min_slug_length: int = Field(default=4, ge=3, le=20)
    max_candidates: int = Field(default=3, ge=1, le=10)
    strip_prefixes: list[str] = Field(default_factory=list)
    stopwords: list[str] = Field(default_factory=list)
    name_noise: list[str] = Field(default_factory=list)

    @field_validator("providers")
    @classmethod
    def _json_only(cls, value: list[AtsProvider]) -> list[AtsProvider]:
        if not set(value) <= set(JSON_ATS_PROVIDERS):
            raise ValueError(
                f"ats_probe.providers must be among {[p.value for p in JSON_ATS_PROVIDERS]}"
            )
        return value


class AtsSearchConfig(BaseModel):
    """``ats_search``: query templates for ``scraper discover`` (ADR 007)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_results_per_query: int = Field(default=10, ge=1, le=50)
    default_cities: list[str] = Field(default_factory=list)
    include_india_query: bool = True
    queries: dict[AtsProvider, list[str]] = Field(default_factory=dict)

    @field_validator("queries")
    @classmethod
    def _templates(cls, value: dict[AtsProvider, list[str]]) -> dict[AtsProvider, list[str]]:
        for provider, templates in value.items():
            if provider not in JSON_ATS_PROVIDERS:
                raise ValueError(f"ats_search.queries: {provider} has no public JSON API")
            for template in templates:
                if "{city}" not in template:
                    raise ValueError(f"query template needs '{{city}}': {template!r}")
        return value


class HimalayasConfig(BaseModel):
    """Himalayas public search API (ADR 010). Each query is fetched once: page 1 only.

    Later pages need ``?page=N``, which himalayas.app's robots.txt disallows, so ``page`` /
    ``offset`` / ``cursor`` keys are rejected here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    search_url: str = Field(pattern=r"^https://himalayas\.app/jobs/api/search$")
    queries: list[dict[str, str]] = Field(min_length=1)

    @field_validator("queries")
    @classmethod
    def _no_paging(cls, value: list[dict[str, str]]) -> list[dict[str, str]]:
        for query in value:
            banned = {"page", "offset", "cursor"} & {k.lower() for k in query}
            if banned:
                raise ValueError(f"himalayas queries must not page (robots.txt): {sorted(banned)}")
        return value


class RemotiveConfig(BaseModel):
    """Remotive public RSS feed (ADR 010). The JSON API (``/api/*``) is robots-disallowed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feed_url: str = Field(pattern=r"^https://remotive\.com/remote-jobs/feed")


class RemoteFeedsConfig(BaseModel):
    """``remote_feeds``: remote-job sources and when a role counts as open to India (ADR 010).

    A role is kept when it is an internship **and** one of these holds:
    - it is restricted to a country in ``india_country_names``;
    - its only timezone is ``india_utc_offset`` (IST);
    - it is worldwide (no country restriction, or a ``worldwide_keywords`` match) and
      ``keep_worldwide`` is set.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    himalayas: HimalayasConfig | None = None
    remotive: RemotiveConfig | None = None
    india_country_names: list[str] = Field(default_factory=lambda: ["India"], min_length=1)
    india_utc_offset: float = 5.5
    worldwide_keywords: list[str] = Field(default_factory=list)
    keep_worldwide: bool = True


def _portal_paths(prefix: str, value: dict[str, str]) -> dict[str, str]:
    for key, path in value.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise ValueError(f"target key must be a city slug or 'remote': {key!r}")
        if not path.startswith(prefix) or "?" in path or "#" in path:
            raise ValueError(f"target path must start with {prefix!r} and have no query: {path!r}")
    return value


class InternshalaConfig(BaseModel):
    """Internshala public listing pages (ADR 011). Keys are city slugs or ``remote``.

    robots.txt allows ``/internships/…`` and ``/page-N/``. It disallows query strings, search
    and ``/internship/details/``, so only listing paths without a query are accepted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(pattern=r"^https://internshala\.com$")
    targets: dict[str, str] = Field(min_length=1)
    max_pages: int = Field(default=2, ge=1, le=5)

    @field_validator("targets")
    @classmethod
    def _paths(cls, value: dict[str, str]) -> dict[str, str]:
        return _portal_paths("/internships/", value)


class CutshortConfig(BaseModel):
    """Cutshort public listing pages (ADR 011). Page 1 of each, with no query string."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(pattern=r"^https://cutshort\.io$")
    targets: dict[str, str] = Field(min_length=1)

    @field_validator("targets")
    @classmethod
    def _paths(cls, value: dict[str, str]) -> dict[str, str]:
        return _portal_paths("/jobs/", value)


class InstahyreConfig(BaseModel):
    """Instahyre internship landing page plus the ``next`` links it embeds (ADR 011)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    landing_url: str = Field(pattern=r"^https://www\.instahyre\.com/[a-z0-9-]+/$")
    max_pages: int = Field(default=5, ge=1, le=10)


class StudentPortalsConfig(BaseModel):
    """``student_portals``: Indian internship portals with unauthenticated public listings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    internshala: InternshalaConfig | None = None
    cutshort: CutshortConfig | None = None
    instahyre: InstahyreConfig | None = None


class SourcesConfig(BaseModel):
    """Root of ``config/sources.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    internship: InternshipConfig
    ats: dict[AtsProvider, AtsSourceConfig]
    ats_embed_hosts: dict[str, list[str]] = Field(default_factory=dict)
    funding_feeds: list[FeedConfig] = Field(default_factory=list)
    incubators: list[IncubatorConfig] = Field(default_factory=list)
    hacker_news: HackerNewsConfig
    non_company_hosts: list[str] = Field(default_factory=list)
    site: SiteConfig
    cold_pitch_phrases: list[str] = Field(default_factory=list)
    contacts: ContactsConfig
    ats_probe: AtsProbeConfig = Field(default_factory=AtsProbeConfig)
    ats_search: AtsSearchConfig = Field(default_factory=AtsSearchConfig)
    yc_india: YcDirectoryConfig | None = None
    remote_feeds: RemoteFeedsConfig = Field(default_factory=RemoteFeedsConfig)
    student_portals: StudentPortalsConfig = Field(default_factory=StudentPortalsConfig)

    @field_validator("ats")
    @classmethod
    def _all_providers(
        cls, value: dict[AtsProvider, AtsSourceConfig]
    ) -> dict[AtsProvider, AtsSourceConfig]:
        missing = set(ATS_PROVIDERS) - set(value)
        if missing:
            raise ValueError(f"ats section is missing providers: {sorted(missing)}")
        unpolled = set(value) - set(POLLED_ATS_PROVIDERS)
        if unpolled:
            raise ValueError(f"ats section has providers without a collector: {sorted(unpolled)}")
        zoho = value.get(AtsProvider.ZOHORECRUIT)
        bad = (
            [c.slug for c in zoho.companies if not ZOHO_PORTAL_HOST.fullmatch(c.slug)]
            if zoho
            else []
        )
        if bad:
            raise ValueError(
                f"zohorecruit slugs must be portal hosts (x.zohorecruit.in|com): {bad}"
            )
        return value

    @field_validator("incubators")
    @classmethod
    def _unique_incubators(cls, value: list[IncubatorConfig]) -> list[IncubatorConfig]:
        names = [i.name for i in value]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate incubator names: {names}")
        return value

    @classmethod
    def load(cls, config_dir: Path | None = None) -> Self:
        """Load and validate ``<config_dir>/sources.yaml``."""
        return cls.model_validate(load_yaml("sources", config_dir))
