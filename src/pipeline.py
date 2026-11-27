"""Orchestration: run collectors and enrichment, and persist results through the ``Repository``.

This is the only place where collector/enrichment output meets storage (collectors never touch
SQLite). Persistence is per item and enrichment is per startup: one bad record or one broken
website is logged and counted, never fatal.
"""

import asyncio
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from src.collectors.ats_search import DiscoveryResult, discover_boards
from src.collectors.config import SourcesConfig
from src.collectors.registry import build_collectors, discovered_seeds
from src.core.base import BaseCollector, CollectorResult, run_all
from src.core.firecrawl_client import HtmlRenderer
from src.core.http import FetchError, HttpClient
from src.core.search import SearchBackend
from src.enrichment.ats_discovery import analyze_site
from src.enrichment.ats_probe import probe_startup
from src.enrichment.contacts import (
    MxResolver,
    dedupe,
    extract_direct_contacts,
    github_contacts,
    pattern_contacts,
)
from src.enrichment.github_signals import GithubSignals, fetch_github_signals
from src.enrichment.scorer import HiddenOpportunityScorer, ScoreInputs, ScoringConfig
from src.enrichment.site import fetch_site_pages
from src.models import AtsProvider, CollectedOpportunity, Startup, StartupLead
from src.models.base import utc_now
from src.storage import Repository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PersistReport:
    """What one collector's results did to the database."""

    collector: str
    startups: int = 0
    opportunities: int = 0
    leads: int = 0
    closed: int = 0
    failed: int = 0
    collector_issues: int = 0
    errors: list[str] = field(default_factory=list)


def persist_results(
    results: Sequence[CollectorResult[Any]],
    repo: Repository,
    *,
    checked_at: datetime | None = None,
) -> list[PersistReport]:
    """Upsert each result's startups (once per domain), opportunities and leads.

    ``checked_at`` stamps ``Startup.last_checked_at`` (default: now, UTC). Items that are neither
    ``CollectedOpportunity`` nor ``StartupLead`` are counted as failures (a collector bug).

    Afterwards, for every board that was fetched and parsed successfully
    (``CollectorResult.closable_scopes``), open opportunities of that board that were not seen in
    this run are marked ``closed`` — unless any item of the result failed to persist.
    """
    checked_at = checked_at or utc_now()
    reports: list[PersistReport] = []
    for result in results:
        report = PersistReport(result.collector, collector_issues=len(result.issues))
        startup_ids: dict[str, int] = {}
        seen_ids: set[int] = set()
        for item in result.items:
            try:
                if isinstance(item, StartupLead):
                    _persist_lead(item, repo, startup_ids, checked_at, report)
                    continue
                if not isinstance(item, CollectedOpportunity):
                    raise TypeError(
                        f"expected CollectedOpportunity or StartupLead, got {type(item).__name__}"
                    )
                startup_id: int | None = None
                if item.startup is not None:
                    domain = item.startup.canonical_domain
                    if domain not in startup_ids:
                        stamped = item.startup.model_copy(update={"last_checked_at": checked_at})
                        startup_ids[domain] = repo.upsert_startup(stamped)
                        report.startups += 1
                    startup_id = startup_ids[domain]
                seen_ids.add(
                    repo.upsert_opportunity(
                        item.opportunity.model_copy(update={"startup_id": startup_id})
                    )
                )
                report.opportunities += 1
            except Exception as exc:
                report.failed += 1
                report.errors.append(f"{type(exc).__name__}: {exc}"[:300])
                logger.exception("pipeline.persist_failed", extra={"collector": result.collector})
        if report.failed == 0:  # never close based on a partially persisted run
            for scope in result.closable_scopes:
                report.closed += repo.close_missing_opportunities(
                    source=result.collector,
                    startup_domain=None if scope == "*" else scope,
                    keep_ids=seen_ids,
                )
        logger.info(
            "pipeline.persisted",
            extra={
                "collector": report.collector,
                "startups": report.startups,
                "opportunities": report.opportunities,
                "leads": report.leads,
                "closed": report.closed,
                "failed": report.failed,
                "collector_issues": report.collector_issues,
            },
        )
        reports.append(report)
    return reports


def _persist_lead(
    lead: StartupLead,
    repo: Repository,
    startup_ids: dict[str, int],
    checked_at: datetime,
    report: PersistReport,
) -> None:
    """Upsert a lead; when it has a domain, also upsert (and link) the ``Startup`` it describes."""
    startup = lead.to_startup()
    if startup is not None:
        domain = startup.canonical_domain
        if domain not in startup_ids:
            startup_ids[domain] = repo.upsert_startup(
                startup.model_copy(update={"last_checked_at": checked_at})
            )
            report.startups += 1
        lead = lead.model_copy(update={"startup_id": startup_ids[domain]})
    repo.upsert_lead(lead)
    report.leads += 1


async def collect_and_store(
    collectors: Sequence[BaseCollector[Any, Any, Any]],
    repo: Repository,
) -> tuple[list[CollectorResult[Any]], list[PersistReport]]:
    """Run all collectors (fault-isolated) and persist everything they produced."""
    results = await run_all(collectors)
    return results, persist_results(results, repo)


# ---------------------------------------------------------------------------- enrichment
@dataclass(slots=True)
class EnrichmentReport:
    """What enrichment learned about one startup."""

    domain: str
    startup_id: int | None
    score: int | None = None
    label: str | None = None
    reasons: list[str] = field(default_factory=list)
    ats: list[str] = field(default_factory=list)
    contacts: int = 0
    github_org: str | None = None
    open_to_cold_pitch: bool | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EnrichmentContext:
    """Shared dependencies for :func:`enrich_startup` / :func:`enrich_all`."""

    http: HttpClient
    sources: SourcesConfig
    scoring: ScoringConfig
    resolver: MxResolver
    renderer: HtmlRenderer | None = None
    github_token: str | None = None
    target_cities: frozenset[str] | None = None
    today: date | None = None


async def enrich_startup(
    startup: Startup, repo: Repository, ctx: EnrichmentContext
) -> EnrichmentReport:
    """Site discovery -> GitHub signals -> 3-pass contacts -> score; persists everything.

    Network failures in optional steps (GitHub) are recorded in ``report.errors``; the startup is
    still scored with whatever was learned.
    """
    if startup.id is None:
        raise ValueError("enrich_startup needs a stored startup (with id)")
    domain = startup.canonical_domain
    report = EnrichmentReport(domain=domain, startup_id=startup.id)
    site, contacts_cfg = ctx.sources.site, ctx.sources.contacts
    confidence = ctx.scoring.contact_confidence
    github_cfg = ctx.scoring.signals.github_activity

    pages = await fetch_site_pages(
        ctx.http,
        domain,
        site.careers_paths,
        max_pages=site.max_pages,
        spa_min_text_chars=site.spa_min_text_chars,
        renderer=ctx.renderer,
    )
    discovery = analyze_site(
        pages,
        cold_pitch_phrases=ctx.sources.cold_pitch_phrases,
        ats_embed_hosts=ctx.sources.ats_embed_hosts,
    )
    report.ats = [f"{m.provider}:{m.slug}" for m in discovery.ats]

    org = startup.github_org or (discovery.github_orgs[0] if discovery.github_orgs else None)
    signals: GithubSignals | None = None
    if org:
        try:
            signals = await fetch_github_signals(
                ctx.http,
                org,
                token=ctx.github_token,
                now=datetime.combine(ctx.today, time(12), UTC) if ctx.today else None,
                recent_commit_days=github_cfg.recent_commit_days,
                active_repo_days=github_cfg.active_repo_days,
            )
        except FetchError as exc:
            report.errors.append(f"github: {exc}"[:200])

    contacts = extract_direct_contacts(
        pages,
        startup_id=startup.id,
        domain=domain,
        hiring_prefixes=contacts_cfg.hiring_mailbox_prefixes,
        excluded_prefixes=contacts_cfg.excluded_mailbox_prefixes,
        profile_hosts=contacts_cfg.profile_hosts,
        confidence=confidence.direct_public,
        # Only the org used for signals is "not a person"; other github.com links on team pages
        # are usually people (founders' personal profiles).
        exclude_profiles=[f"https://github.com/{org}"] if org else [],
    )
    if signals is not None:
        contacts += await github_contacts(
            ctx.http,
            signals.org,
            startup_id=startup.id,
            domain=domain,
            org_email=signals.org_email,
            token=ctx.github_token,
            max_members=contacts_cfg.max_github_members,
            confidence=confidence.github_public,
        )
    founders = [f for lead in repo.list_leads(startup_id=startup.id) for f in lead.founders]
    contacts += await pattern_contacts(
        founders,
        startup_id=startup.id,
        domain=domain,
        templates=contacts_cfg.pattern_templates,
        resolver=ctx.resolver,
        confidence=confidence.pattern_mx_verified,
    )
    contacts = dedupe(contacts)
    repo.upsert_contacts(contacts)
    report.contacts = len(contacts)

    primary = discovery.primary_tier1 or (discovery.ats[0] if discovery.ats else None)
    update: dict[str, Any] = {"last_checked_at": utc_now()}
    if discovery.pages_ok:
        update["open_to_cold_pitch"] = discovery.open_to_cold_pitch
    if signals is not None:
        update["github_org"] = signals.org
        update["tech_stack"] = [*startup.tech_stack, *(lang.lower() for lang in signals.languages)]
    if primary is not None and startup.ats_provider is None:
        update["ats_provider"], update["ats_slug"] = AtsProvider(primary.provider), primary.slug
    elif primary is None and startup.ats_provider is None and ctx.sources.ats_probe.enabled:
        # No board known and none embedded on the careers pages: guess + verify slugs (ADR 007).
        probe = ctx.sources.ats_probe
        found = await probe_startup(
            ctx.http,
            domain=domain,
            name=startup.name,
            endpoints={p.value: c.endpoint for p, c in ctx.sources.ats.items()},
            rules=probe,
            providers=[p.value for p in probe.providers],
        )
        if found is not None:
            update["ats_provider"], update["ats_slug"] = AtsProvider(found.provider), found.slug
            report.ats.append(f"{found.provider}:{found.slug} (probed)")
    updated = startup.model_validate(startup.model_dump() | update)

    scorer = HiddenOpportunityScorer(ctx.scoring, target_cities=ctx.target_cities, today=ctx.today)
    breakdown = scorer.score(
        ScoreInputs.from_startup(
            updated,
            github=signals,
            contacts=repo.list_contacts(startup_id=startup.id),
            opportunities=repo.list_opportunities(startup_id=startup.id),
        )
    )
    repo.upsert_startup(updated.model_copy(update={"hidden_score": breakdown.total}))
    report.score, report.label, report.reasons = breakdown.total, breakdown.label, breakdown.reasons
    report.github_org = updated.github_org
    report.open_to_cold_pitch = updated.open_to_cold_pitch
    logger.info(
        "pipeline.enriched",
        extra={"domain": domain, "score": breakdown.total, "contacts": report.contacts,
               "ats": ",".join(report.ats) or None},
    )  # fmt: skip
    return report


async def enrich_all(
    repo: Repository,
    ctx: EnrichmentContext,
    *,
    startups: Sequence[Startup] | None = None,
    concurrency: int = 5,
) -> list[EnrichmentReport]:
    """Enrich many startups concurrently; one startup's failure never stops the others."""
    targets = list(startups) if startups is not None else repo.list_startups()
    gate = asyncio.Semaphore(max(1, min(concurrency, 5)))

    async def one(startup: Startup) -> EnrichmentReport:
        async with gate:
            try:
                return await enrich_startup(startup, repo, ctx)
            except Exception as exc:
                logger.exception(
                    "pipeline.enrich_failed", extra={"domain": startup.canonical_domain}
                )
                return EnrichmentReport(
                    domain=startup.canonical_domain,
                    startup_id=startup.id,
                    errors=[f"{type(exc).__name__}: {exc}"[:300]],
                )

    return list(await asyncio.gather(*(one(s) for s in targets)))


# ---------------------------------------------------------------------------- CLI entry points
async def run_scrape(
    repo: Repository,
    http: HttpClient,
    *,
    names: Sequence[str] | None = None,
    config_dir: Path | None = None,
    renderer: HtmlRenderer | None = None,
    cities: Collection[str] | None = None,
) -> tuple[list[CollectorResult[Any]], list[PersistReport]]:
    """``scraper scrape``: build collectors (YAML seeds + boards auto-discovered on careers pages),
    run them fault-isolated, persist, and close postings that vanished from their boards."""
    collectors = build_collectors(
        http,
        names,
        config_dir=config_dir,
        renderer=renderer,
        extra_seeds=discovered_seeds(repo.list_startups()),
        cities=cities,
    )
    return await collect_and_store(collectors, repo)


def plan_scrape(
    repo: Repository,
    http: HttpClient,
    *,
    names: Sequence[str] | None = None,
    config_dir: Path | None = None,
    cities: Collection[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """``scraper scrape --dry-run``: ``(collector, [target labels])`` without any network call."""
    collectors = build_collectors(
        http,
        names,
        config_dir=config_dir,
        extra_seeds=discovered_seeds(repo.list_startups()),
        cities=cities,
    )
    return [(c.name, [c.describe_target(t) for t in c.targets()]) for c in collectors]


def select_for_enrichment(repo: Repository, limit: int | None = None) -> list[Startup]:
    """Never-scored startups first, then the least recently checked."""
    oldest = datetime.min.replace(tzinfo=UTC)
    startups = sorted(
        repo.list_startups(),
        key=lambda s: (s.hidden_score is not None, s.last_checked_at or oldest, s.id or 0),
    )
    return startups[:limit] if limit is not None else startups


# ---------------------------------------------------------------------------- ATS discovery
@dataclass(slots=True)
class DiscoverReport:
    """What ``scraper discover`` persisted."""

    saved_startups: int = 0
    saved_leads: int = 0
    not_found: int = 0
    conflicts: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)


def known_boards(repo: Repository, sources: SourcesConfig) -> set[tuple[str, str]]:
    """``(provider, slug)`` already polled: YAML seeds + boards stored on startups."""
    known = {(p.value, c.slug.lower()) for p, src in sources.ats.items() for c in src.companies}
    for startup in repo.list_startups():
        if startup.ats_provider is not None and startup.ats_slug:
            known.add((startup.ats_provider.value, startup.ats_slug.lower()))
    return known


async def run_discover(
    repo: Repository,
    http: HttpClient,
    backend: SearchBackend,
    sources: SourcesConfig,
    *,
    providers: Sequence[str],
    city_names: Sequence[str],
    limit: int = 20,
) -> tuple[DiscoveryResult, DiscoverReport]:
    """``scraper discover``: find + verify boards, store them in SQLite (never in sources.yaml).

    A verified board on a startup whose domain is already stored with a *different* board is
    reported as a conflict and not overwritten.
    """
    result = await discover_boards(
        http,
        backend,
        sources,
        providers=providers,
        city_names=city_names,
        known=known_boards(repo, sources),
        limit=limit,
    )
    report = DiscoverReport()
    for board in result.boards:
        row = {"provider": board.candidate.provider, "slug": board.candidate.slug,
               "status": board.status, "domain": None,
               "jobs": board.snapshot.job_count if board.snapshot else None}  # fmt: skip
        if board.startup is not None:
            existing = repo.get_startup_by_domain(board.startup.canonical_domain)
            row["domain"] = board.startup.canonical_domain
            if (
                existing is not None
                and existing.ats_provider is not None
                and (
                    existing.ats_provider,
                    (existing.ats_slug or "").lower(),
                )
                != (board.startup.ats_provider, board.candidate.slug)
            ):
                row["status"] = "conflict"
                report.conflicts += 1
            else:
                repo.upsert_startup(board.startup)
                report.saved_startups += 1
        elif board.lead is not None:
            repo.upsert_lead(board.lead)
            report.saved_leads += 1
        else:
            report.not_found += 1
        report.rows.append(row)
    logger.info(
        "pipeline.discovered",
        extra={"startups": report.saved_startups, "leads": report.saved_leads,
               "not_found": report.not_found, "conflicts": report.conflicts},
    )  # fmt: skip
    return result, report
