"""Search-based discovery of public ATS boards (``scraper discover``, ADR 007).

Flow: ``sources.yaml: ats_search`` templates × cities → official search API (Brave / Firecrawl)
→ ``(provider, slug)`` pairs parsed from result URLs → skip boards already in SQLite → verify each
on the live public ATS JSON API → infer the company's own domain from the board's postings.

Returns validated models only (collectors never write SQLite): a verified board with a plausible
domain becomes a ``Startup`` (``ats_provider``/``ats_slug`` set, ``discovered_via="ats_search"``),
which ``discovered_seeds()`` then polls on every ``scraper scrape``; a verified board whose domain
can't be inferred becomes a ``StartupLead`` for human review instead of an invented domain.
"""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Literal

from src.collectors.config import SourcesConfig
from src.core.http import FetchError, HttpClient
from src.core.search import SearchBackend, SearchHit
from src.enrichment.ats_discovery import find_ats
from src.enrichment.ats_probe import BoardSnapshot, fetch_board, infer_domain
from src.models import AtsProvider, Startup, StartupLead

logger = logging.getLogger(__name__)

Status = Literal["startup", "lead", "not_found"]


@dataclass(frozen=True, slots=True)
class BoardCandidate:
    """A board referenced by a search result URL."""

    provider: str
    slug: str
    found_url: str


@dataclass(slots=True)
class DiscoveredBoard:
    """Verification outcome for one candidate."""

    candidate: BoardCandidate
    status: Status
    snapshot: BoardSnapshot | None = None
    startup: Startup | None = None
    lead: StartupLead | None = None


@dataclass(slots=True)
class DiscoveryResult:
    """Everything one ``discover`` run did (persisted by ``pipeline.run_discover``)."""

    backend: str
    queries: list[str] = field(default_factory=list)
    hits: int = 0
    candidates: list[BoardCandidate] = field(default_factory=list)
    skipped_known: int = 0
    boards: list[DiscoveredBoard] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def build_queries(
    sources: SourcesConfig, *, providers: Sequence[str], city_names: Sequence[str]
) -> list[tuple[str, str]]:
    """``(provider, query)`` pairs from ``ats_search.queries`` templates × city display names."""
    cfg = sources.ats_search
    places = [*city_names, *(["India"] if cfg.include_india_query else [])]
    pairs: list[tuple[str, str]] = []
    for provider in providers:
        for template in cfg.queries.get(AtsProvider(provider), []):
            for place in dict.fromkeys(places):
                pairs.append((provider, template.format(city=place)))
    return pairs


def extract_candidates(
    hits: Sequence[SearchHit], *, providers: Collection[str]
) -> list[BoardCandidate]:
    """Unique ``(provider, slug)`` boards referenced by result URLs (job pages included)."""
    seen: dict[tuple[str, str], BoardCandidate] = {}
    for hit in hits:
        for provider, slug in find_ats(hit.url, enabled_providers=providers):
            seen.setdefault((provider, slug), BoardCandidate(provider, slug, hit.url))
    return list(seen.values())


def _company_name(snapshot: BoardSnapshot) -> str:
    if snapshot.company_name:
        return snapshot.company_name
    return snapshot.slug.replace("-", " ").replace("_", " ").title()


async def discover_boards(
    http: HttpClient,
    backend: SearchBackend,
    sources: SourcesConfig,
    *,
    providers: Sequence[str],
    city_names: Sequence[str],
    known: Collection[tuple[str, str]] = (),
    limit: int = 20,
) -> DiscoveryResult:
    """Search, extract, verify; at most ``limit`` new boards are verified per run."""
    result = DiscoveryResult(backend=backend.name)
    known_set = {(p, s.lower()) for p, s in known}
    endpoints = {p.value: c.endpoint for p, c in sources.ats.items()}
    hits: list[SearchHit] = []
    for _provider, query in build_queries(sources, providers=providers, city_names=city_names):
        result.queries.append(query)
        try:
            found = await backend.search(query, sources.ats_search.max_results_per_query)
        except FetchError as exc:
            result.errors.append(f"search {query!r}: {exc}"[:200])
            continue
        hits.extend(found)
    result.hits = len(hits)

    for candidate in extract_candidates(hits, providers=providers):
        if (candidate.provider, candidate.slug) in known_set:
            result.skipped_known += 1
            continue
        result.candidates.append(candidate)

    for candidate in result.candidates[:limit]:
        snapshot = await fetch_board(http, candidate.provider, candidate.slug,
                                     endpoints[candidate.provider])  # fmt: skip
        if snapshot is None:
            result.boards.append(DiscoveredBoard(candidate, "not_found"))
            continue
        name = _company_name(snapshot)
        domain = infer_domain(
            snapshot,
            non_company_hosts=sources.non_company_hosts,
            name_noise=sources.ats_probe.name_noise,
        )
        if domain is None:
            lead = StartupLead(name=name, source="ats_search", source_url=snapshot.board_url)
            result.boards.append(DiscoveredBoard(candidate, "lead", snapshot, lead=lead))
            continue
        startup = Startup(
            name=name,
            canonical_domain=domain,
            website_url=f"https://{domain}",
            ats_provider=AtsProvider(candidate.provider),
            ats_slug=candidate.slug,
            discovered_via="ats_search",
        )
        result.boards.append(DiscoveredBoard(candidate, "startup", snapshot, startup=startup))
    logger.info(
        "ats_search.done",
        extra={"backend": backend.name, "queries": len(result.queries), "hits": result.hits,
               "candidates": len(result.candidates), "skipped_known": result.skipped_known},
    )  # fmt: skip
    return result
