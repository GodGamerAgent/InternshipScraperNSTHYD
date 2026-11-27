"""Manual, user-triggered pipeline runs for the UI (the dashboard itself never touches the network).

``scrape_job`` / ``enrich_job`` wrap ``src.pipeline`` exactly like the CLI does, stream the
pipeline's structured log events to an ``on_line`` callback (shown live in the page), and never
raise: failures come back as ``JobResult(ok=False, error=…)``. ``JOB_LOCK`` allows one job at a
time so two clicks can't write the database concurrently. No NiceGUI import here (tested in CI).
"""

import asyncio
import logging
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.collectors.config import SourcesConfig
from src.core.config import Settings
from src.core.firecrawl_client import FirecrawlClient
from src.core.http import HttpClient
from src.core.log import record_fields
from src.core.search import search_backend_from_settings, search_backend_name
from src.enrichment.contacts import DnsMxResolver, MxResolver
from src.enrichment.geo import GeoResolver
from src.enrichment.scorer import ScoringConfig
from src.pipeline import (
    EnrichmentContext,
    enrich_all,
    run_discover,
    run_scrape,
    select_for_enrichment,
)
from src.storage import Repository

logger = logging.getLogger(__name__)

JOB_LOCK = asyncio.Lock()
LineCallback = Callable[[str], Any]
HttpFactory = Callable[[Settings], HttpClient]


@dataclass(slots=True)
class JobResult:
    """Outcome of one UI-triggered run."""

    ok: bool
    summary: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class _LineHandler(logging.Handler):
    def __init__(self, on_line: LineCallback) -> None:
        super().__init__(logging.INFO)
        self.on_line = on_line

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = " ".join(f"{k}={v}" for k, v in record_fields(record).items() if v is not None)
            self.on_line(f"{record.levelname:<7} {record.getMessage()} {fields}".rstrip())
        except Exception:  # a broken UI callback must never break the pipeline
            self.handleError(record)


@contextmanager
def capture_logs(on_line: LineCallback | None) -> Iterator[None]:
    """Forward INFO+ events from the ``src`` loggers to ``on_line`` for the duration."""
    if on_line is None:
        yield
        return
    src_logger = logging.getLogger("src")
    handler, previous = _LineHandler(on_line), src_logger.level
    src_logger.addHandler(handler)
    src_logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        src_logger.removeHandler(handler)
        src_logger.setLevel(previous)


def is_running() -> bool:
    return JOB_LOCK.locked()


async def scrape_job(
    settings: Settings,
    *,
    names: Sequence[str] | None = None,
    cities: Collection[str] | None = None,
    http_factory: HttpFactory = HttpClient.from_settings,
    on_line: LineCallback | None = None,
) -> JobResult:
    """Run ``scraper scrape`` in-process (robots, rate limits and retries all apply)."""
    async with JOB_LOCK:
        with capture_logs(on_line):
            try:
                async with http_factory(settings) as http:
                    with Repository.open(settings.db_path) as repo:
                        results, reports = await run_scrape(
                            repo,
                            http,
                            names=list(names) if names else None,
                            config_dir=settings.config_dir,
                            renderer=FirecrawlClient.from_settings(http, settings),
                            cities=cities,
                        )
            except Exception as exc:
                logger.exception("ui.scrape_failed")
                return JobResult(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
    summary = [
        {"collector": r.collector, "targets": r.targets_total, "failed": r.targets_failed,
         "opportunities": p.opportunities, "leads": p.leads, "startups": p.startups,
         "closed": p.closed, "errors": p.failed}
        for r, p in zip(results, reports, strict=True)
    ]  # fmt: skip
    total = sum(r.targets_total for r in results)
    failed = sum(r.targets_failed for r in results)
    return JobResult(ok=not (total and failed == total), summary=summary)


async def enrich_job(
    settings: Settings,
    *,
    limit: int | None = 10,
    http_factory: HttpFactory = HttpClient.from_settings,
    resolver_factory: Callable[[], MxResolver] = DnsMxResolver,
    on_line: LineCallback | None = None,
) -> JobResult:
    """Run ``scraper enrich --limit N`` in-process (DNS MX only; Firecrawl only with a key)."""
    async with JOB_LOCK:
        with capture_logs(on_line):
            try:
                async with http_factory(settings) as http:
                    with Repository.open(settings.db_path) as repo:
                        context = EnrichmentContext(
                            http=http,
                            sources=SourcesConfig.load(settings.config_dir),
                            scoring=ScoringConfig.load(settings.config_dir),
                            resolver=resolver_factory(),
                            renderer=FirecrawlClient.from_settings(http, settings),
                            github_token=settings.github_token,
                            today=datetime.now(UTC).date(),
                        )
                        reports = await enrich_all(
                            repo, context, startups=select_for_enrichment(repo, limit)
                        )
            except Exception as exc:
                logger.exception("ui.enrich_failed")
                return JobResult(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
    summary = [
        {"domain": r.domain, "score": r.score, "label": r.label, "contacts": r.contacts,
         "ats": ", ".join(r.ats), "errors": len(r.errors)}
        for r in reports
    ]  # fmt: skip
    return JobResult(ok=not reports or any(r.score is not None for r in reports), summary=summary)


NO_SEARCH_KEY = (
    "No search API key configured: set BRAVE_SEARCH_API_KEY or FIRECRAWL_API_KEY in .env and "
    "restart `scraper ui`. Nothing was searched."
)


async def discover_job(
    settings: Settings,
    *,
    limit: int = 20,
    http_factory: HttpFactory = HttpClient.from_settings,
    on_line: LineCallback | None = None,
) -> JobResult:
    """Run ``scraper discover`` with the configured defaults (all providers, default cities)."""
    if search_backend_name(settings) is None:
        return JobResult(ok=False, error=NO_SEARCH_KEY)  # no network calls at all
    async with JOB_LOCK:
        with capture_logs(on_line):
            try:
                sources = SourcesConfig.load(settings.config_dir)
                geo = GeoResolver.from_config(settings.config_dir)
                city_names = [geo.city(c).display_name for c in sources.ats_search.default_cities]
                async with http_factory(settings) as http:
                    backend = search_backend_from_settings(http, settings)
                    assert backend is not None
                    with Repository.open(settings.db_path) as repo:
                        result, report = await run_discover(
                            repo,
                            http,
                            backend,
                            sources,
                            providers=[p.value for p in sources.ats_probe.providers],
                            city_names=city_names,
                            limit=limit,
                        )
            except Exception as exc:
                logger.exception("ui.discover_failed")
                return JobResult(ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
    return JobResult(ok=not (result.errors and not result.hits), summary=report.rows)
