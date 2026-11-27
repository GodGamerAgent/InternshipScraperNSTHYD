"""Typer CLI (``uv run scraper ...``) — argument parsing and output only.

Business logic lives in ``src/pipeline.py`` (scrape / enrich), ``src/exports.py`` (rows, CSV/JSON,
stats) and ``src/storage`` (queries). Commands::

    scraper scrape  [--sources greenhouse,thub,…] [--cities hyderabad,…] [--dry-run]
    scraper enrich  [--limit N] [--cities …]
    scraper export  [--what opportunities|startups|leads|contacts] [--cities …] [--include-remote]
                    [--work-mode onsite,hybrid,…] [--min-hidden-score N] [--status open|closed|all]
                    [--format csv|json] [--out PATH|-]
    scraper stats   [--json]
    scraper discover [--provider all|greenhouse|…] [--cities …] [--limit N] [--dry-run]
    scraper ui      [--host 127.0.0.1] [--port 8080] [--open/--no-open]  (uv sync --extra ui)

Global options (before the command): ``--db PATH``, ``--config-dir DIR``, ``--log-level LEVEL``.
"""

import asyncio
import dataclasses
import importlib.util
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer

from src import __version__
from src.collectors.ats_search import build_queries
from src.collectors.config import SourcesConfig
from src.collectors.registry import ALL_COLLECTOR_NAMES
from src.core.config import Settings
from src.core.firecrawl_client import FirecrawlClient
from src.core.http import HttpClient
from src.core.log import configure_logging
from src.core.search import search_backend_from_settings, search_backend_name
from src.enrichment.contacts import DnsMxResolver, MxResolver
from src.enrichment.geo import GeoResolver
from src.enrichment.scorer import ScoringConfig
from src.exports import build_rows, default_export_path, render, summarize_stats, write_export
from src.models import OpportunityStatus, WorkMode
from src.pipeline import (
    EnrichmentContext,
    enrich_all,
    plan_scrape,
    run_discover,
    run_scrape,
    select_for_enrichment,
)
from src.storage import ExportFilter, Repository

app = typer.Typer(
    name="scraper",
    help="India startup internship & hidden-opportunity scraper.",
    no_args_is_help=True,
    add_completion=False,
)


class What(StrEnum):
    opportunities = "opportunities"
    startups = "startups"
    leads = "leads"
    contacts = "contacts"


class Format(StrEnum):
    csv = "csv"
    json = "json"


class Status(StrEnum):
    open = "open"
    closed = "closed"
    all = "all"


# ---------------------------------------------------------------------------- factories
# Module-level so tests can monkeypatch them (mocked HTTP transport, fake DNS).
def make_http_client(settings: Settings) -> HttpClient:
    """The polite client for a run (file-backed ETag cache in ``SCRAPER_HTTP_CACHE_DIR``)."""
    return HttpClient.from_settings(settings)


def make_mx_resolver() -> MxResolver:
    """DNS MX resolver for contacts Pass 3 (MX lookups only, never SMTP)."""
    return DnsMxResolver()


# ---------------------------------------------------------------------------- parsing helpers
def _split(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [v.strip().lower() for v in value.split(",") if v.strip()]
    return items or None


def _cities(value: str | None, settings: Settings) -> frozenset[str] | None:
    cities = _split(value)
    if cities is None:
        return None
    known = GeoResolver.from_config(settings.config_dir).city_slugs
    unknown = sorted(set(cities) - set(known))
    if unknown:
        raise typer.BadParameter(f"unknown city {unknown}; known: {', '.join(known)}")
    return frozenset(cities)


def _work_modes(value: str | None) -> frozenset[WorkMode] | None:
    modes = _split(value)
    if modes is None:
        return None
    valid = {m.value for m in WorkMode}
    unknown = sorted(set(modes) - valid)
    if unknown:
        raise typer.BadParameter(f"unknown work mode {unknown}; known: {', '.join(sorted(valid))}")
    return frozenset(WorkMode(m) for m in modes)


def _sources(value: str | None) -> list[str] | None:
    names = _split(value)
    if names is None:
        return None
    unknown = sorted(set(names) - set(ALL_COLLECTOR_NAMES))
    if unknown:
        raise typer.BadParameter(
            f"unknown source {unknown}; known: {', '.join(sorted(ALL_COLLECTOR_NAMES))}"
        )
    return names


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj if isinstance(ctx.obj, Settings) else Settings.from_env()


def _table(rows: list[list[Any]], headers: list[str]) -> str:
    cells = [headers, *[["" if v is None else str(v) for v in row] for row in rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


# ---------------------------------------------------------------------------- commands
@app.callback()
def main(
    ctx: typer.Context,
    db: Path | None = typer.Option(None, "--db", help="SQLite file (default SCRAPER_DB_PATH)."),
    config_dir: Path | None = typer.Option(
        None, "--config-dir", help="Directory with the YAML configs (default SCRAPER_CONFIG_DIR)."
    ),
    log_level: str | None = typer.Option(None, "--log-level", help="DEBUG, INFO, WARNING…"),
) -> None:
    """India startup internship & hidden-opportunity scraper."""
    settings = Settings.from_env()
    if db is not None:
        settings = dataclasses.replace(settings, db_path=db.expanduser().resolve())
    if config_dir is not None:
        settings = dataclasses.replace(settings, config_dir=config_dir.expanduser().resolve())
    configure_logging(log_level or settings.log_level, settings.log_format)
    ctx.obj = settings


@app.command()
def version() -> None:
    """Print the installed scraper version."""
    typer.echo(__version__)


@app.command()
def scrape(
    ctx: typer.Context,
    sources: str | None = typer.Option(
        None,
        "--sources",
        "-s",
        help=f"Comma-separated collectors (default: all). Known: {', '.join(ALL_COLLECTOR_NAMES)}.",
    ),
    cities: str | None = typer.Option(
        None, "--cities", "--city", help="Only poll ATS seeds whose HQ is in these cities."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be fetched; no network."
    ),
) -> None:
    """Collect internships and leads, persist them, and close postings that vanished."""
    settings = _settings(ctx)
    names, city_set = _sources(sources), _cities(cities, settings)

    async def go() -> int:
        async with make_http_client(settings) as http:
            if dry_run:
                db = settings.db_path if settings.db_path.exists() else ":memory:"
                with Repository.open(db) as repo:
                    plan = plan_scrape(
                        repo, http, names=names, config_dir=settings.config_dir, cities=city_set
                    )
                for collector, targets in plan:
                    typer.echo(f"{collector} ({len(targets)} targets): {', '.join(targets) or '-'}")
                return 0
            with Repository.open(settings.db_path) as repo:
                results, reports = await run_scrape(
                    repo,
                    http,
                    names=names,
                    config_dir=settings.config_dir,
                    renderer=FirecrawlClient.from_settings(http, settings),
                    cities=city_set,
                )
        rows = [
            [r.collector, r.targets_total, r.targets_failed, p.opportunities, p.leads, p.startups,
             p.closed, p.failed]
            for r, p in zip(results, reports, strict=True)
        ]  # fmt: skip
        headers = [
            "collector",
            "targets",
            "failed",
            "opps",
            "leads",
            "startups",
            "closed",
            "errors",
        ]
        typer.echo(_table(rows, headers))
        for result in results:
            for issue in result.issues:
                typer.echo(
                    f"  ! {result.collector}/{issue.target}: {issue.stage} {issue.error_type}"
                    f" {issue.message[:120]}",
                    err=True,
                )
        total = sum(r.targets_total for r in results)
        failed = sum(r.targets_failed for r in results)
        return 1 if total and failed == total else 0

    raise typer.Exit(asyncio.run(go()))


@app.command()
def enrich(
    ctx: typer.Context,
    limit: int | None = typer.Option(None, "--limit", min=1, help="Enrich at most N startups."),
    cities: str | None = typer.Option(
        None, "--cities", help="Target cities for the location signal (default: any Indian city)."
    ),
) -> None:
    """Careers-page discovery, GitHub signals, public contacts and hidden-opportunity score."""
    settings = _settings(ctx)
    city_set = _cities(cities, settings)

    async def go() -> int:
        async with make_http_client(settings) as http:
            with Repository.open(settings.db_path) as repo:
                startups = select_for_enrichment(repo, limit)
                context = EnrichmentContext(
                    http=http,
                    sources=SourcesConfig.load(settings.config_dir),
                    scoring=ScoringConfig.load(settings.config_dir),
                    resolver=make_mx_resolver(),
                    renderer=FirecrawlClient.from_settings(http, settings),
                    github_token=settings.github_token,
                    target_cities=city_set,
                    today=datetime.now(UTC).date(),
                )
                reports = await enrich_all(repo, context, startups=startups)
        if not reports:
            typer.echo("No startups to enrich yet — run `scraper scrape` first.")
            return 0
        rows = [
            [r.domain, r.score, r.label, r.contacts, ",".join(r.ats) or None, len(r.errors)]
            for r in sorted(reports, key=lambda r: -(r.score or -1))
        ]
        typer.echo(_table(rows, ["domain", "score", "label", "contacts", "ats", "errors"]))
        return 1 if all(r.score is None for r in reports) else 0

    raise typer.Exit(asyncio.run(go()))


@app.command()
def export(
    ctx: typer.Context,
    what: What = typer.Option(What.opportunities, "--what", help="Which records to export."),
    cities: str | None = typer.Option(None, "--cities", "--city", help="e.g. hyderabad,bengaluru"),
    include_remote: bool = typer.Option(
        False, "--include-remote", help="Also include Remote-India roles."
    ),
    work_mode: str | None = typer.Option(
        None, "--work-mode", help="onsite,hybrid,remote_india,remote_global"
    ),
    min_hidden_score: int | None = typer.Option(
        None, "--min-hidden-score", min=0, max=100, help="Startup hidden-opportunity score >= N."
    ),
    status: Status = typer.Option(Status.open, "--status", help="Opportunity status."),
    fmt: Format = typer.Option(Format.csv, "--format", help="csv or json."),
    out: str | None = typer.Option(
        None, "--out", help="Output file, or '-' for stdout. Default exports/<what>_<date>.<fmt>."
    ),
) -> None:
    """Export filtered records as CSV or JSON for human-reviewed applications and outreach."""
    settings = _settings(ctx)
    flt = ExportFilter(
        cities=_cities(cities, settings),
        work_modes=_work_modes(work_mode),
        include_remote=include_remote,
        min_hidden_score=min_hidden_score,
        status=None if status is Status.all else OpportunityStatus(status.value),
    )
    with Repository.open(settings.db_path) as repo:
        rows = build_rows(repo, what.value, flt)  # type: ignore[arg-type]
    text = render(rows, what.value, fmt.value)  # type: ignore[arg-type]
    if out == "-":
        typer.echo(text, nl=False)
        return
    path = Path(out) if out else default_export_path(
        what.value, fmt.value, today=datetime.now(UTC).date()  # type: ignore[arg-type]
    )  # fmt: skip
    write_export(text, path)
    typer.echo(f"Wrote {len(rows)} {what.value} to {path}")


@app.command()
def stats(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Database totals and breakdowns (open roles by city / mode / source, startup scores)."""
    settings = _settings(ctx)
    with Repository.open(settings.db_path) as repo:
        summary = summarize_stats(repo, ScoringConfig.load(settings.config_dir))
    if as_json:
        typer.echo(json.dumps(summary, indent=2))
        return
    counts = summary["counts"]
    typer.echo(_table([[k, v] for k, v in counts.items()], ["table", "rows"]))
    for title, key in (
        ("Opportunities by status", "opportunities_by_status"),
        ("Open roles by city", "open_by_city"),
        ("Open roles by work mode", "open_by_work_mode"),
        ("Open roles by source", "open_by_source"),
        ("Startups by hidden score", "startups_by_score"),
        ("Contacts by method", "contacts_by_method"),
    ):
        typer.echo(f"\n{title}")
        typer.echo(_table([[k, v] for k, v in summary[key].items()], ["value", "count"]))
    typer.echo(
        f"\nLeads: {summary['leads_with_domain']} with a website, "
        f"{summary['leads_without_domain']} without"
    )


class Provider(StrEnum):
    all = "all"
    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    workable = "workable"
    rippling = "rippling"
    breezyhr = "breezyhr"
    recruitee = "recruitee"


SEARCH_KEY_HINT = (
    "No search API key configured, so nothing was searched (0 network calls).\n"
    "Set one of these in .env, then run `uv run --env-file .env scraper discover`:\n"
    "  BRAVE_SEARCH_API_KEY=...   (Brave Search API)\n"
    "  FIRECRAWL_API_KEY=...      (Firecrawl /search)"
)


@app.command()
def discover(
    ctx: typer.Context,
    provider: Provider = typer.Option(Provider.all, "--provider", help="ATS to search for."),
    cities: str | None = typer.Option(
        None,
        "--cities",
        "--city",
        help="City slugs for the queries (default: ats_search.default_cities).",
    ),
    limit: int = typer.Option(20, "--limit", min=1, help="Verify at most N new boards."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the queries; no network calls."),
) -> None:
    """Find new public ATS boards via a search API, verify them, and store them in SQLite."""
    settings = _settings(ctx)
    sources = SourcesConfig.load(settings.config_dir)
    geo = GeoResolver.from_config(settings.config_dir)
    slugs = _cities(cities, settings) or frozenset(sources.ats_search.default_cities)
    city_names = [geo.city(slug).display_name for slug in sorted(slugs)]
    providers = (
        [p.value for p in Provider if p is not Provider.all]
        if provider is Provider.all
        else [provider.value]
    )
    backend_name = search_backend_name(settings)
    if dry_run:
        typer.echo(
            f"backend: {backend_name or 'none (set BRAVE_SEARCH_API_KEY or FIRECRAWL_API_KEY)'}"
        )
        for prov, query in build_queries(sources, providers=providers, city_names=city_names):
            typer.echo(f"{prov:10} {query}")
        return
    if backend_name is None:
        typer.echo(SEARCH_KEY_HINT)
        raise typer.Exit(0)

    async def go() -> int:
        async with make_http_client(settings) as http:
            backend = search_backend_from_settings(http, settings)
            assert backend is not None  # guarded by search_backend_name above
            with Repository.open(settings.db_path) as repo:
                result, report = await run_discover(
                    repo, http, backend, sources, providers=providers, city_names=city_names,
                    limit=limit,
                )  # fmt: skip
        typer.echo(
            f"{result.backend}: {len(result.queries)} queries, {result.hits} results, "
            f"{len(result.candidates)} new boards, {result.skipped_known} already known"
        )
        if report.rows:
            rows = [
                [r["provider"], r["slug"], r["status"], r["domain"], r["jobs"]] for r in report.rows
            ]
            typer.echo(_table(rows, ["provider", "slug", "status", "domain", "jobs"]))
        typer.echo(
            f"saved {report.saved_startups} startups (polled on next `scraper scrape`), "
            f"{report.saved_leads} leads (no domain found), {report.conflicts} conflicts, "
            f"{report.not_found} not found"
        )
        for error in result.errors:
            typer.echo(f"  ! {error}", err=True)
        return 1 if result.errors and not result.hits else 0

    raise typer.Exit(asyncio.run(go()))


WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", "[::]", ""})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@app.command("ui")
def ui_command(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (local only by default)."),
    port: int = typer.Option(8080, "--port", min=1, max=65535, help="HTTP port."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open a browser tab."),
) -> None:
    """Launch the optional local NiceGUI dashboard (needs `uv sync --extra ui`)."""
    if importlib.util.find_spec("nicegui") is None:
        typer.echo(
            "NiceGUI is not installed. Install the optional UI extra with:\n"
            "  uv sync --extra ui\n"
            "then run `uv run scraper ui` (or: uv run --extra ui scraper ui).",
            err=True,
        )
        raise typer.Exit(1)
    if host.strip() in WILDCARD_HOSTS:
        raise typer.BadParameter(
            "refusing to bind to all interfaces; the dashboard shows your local data "
            "(use 127.0.0.1, or a specific LAN address if you really mean it)",
            param_hint="--host",
        )
    if host not in LOOPBACK_HOSTS:
        typer.echo(
            f"Warning: binding to {host} makes the dashboard reachable from your network.", err=True
        )
    from src.ui.app import run_ui  # imported lazily: NiceGUI is an optional extra

    settings = _settings(ctx)
    typer.echo(f"Dashboard on http://{host}:{port}  (DB: {settings.db_path})")
    run_ui(settings, host=host, port=port, show=open_browser)


if __name__ == "__main__":  # pragma: no cover
    app()
