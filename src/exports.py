"""Export rows (CSV / JSON) and the ``stats`` summary — presentation over ``Repository`` queries.

Column sets are fixed per export kind so downstream spreadsheets don't break between runs.
CSV cells that start with ``= + - @`` (or a tab/CR) are prefixed with ``'``: exported text is
scraped from the web and must not be executed as a spreadsheet formula (CSV injection).
"""

import csv
import io
import json
from collections.abc import Sequence
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from src.enrichment.scorer import ScoringConfig
from src.storage import ExportFilter, Repository

ExportKind = Literal["opportunities", "startups", "leads", "contacts"]
ExportFormat = Literal["csv", "json"]
EXPORT_KINDS: tuple[ExportKind, ...] = ("opportunities", "startups", "leads", "contacts")

COLUMNS: dict[ExportKind, tuple[str, ...]] = {
    "opportunities": (
        "title", "company", "company_domain", "city", "state", "work_mode", "country",
        "stipend_inr_min", "stipend_inr_max", "is_paid", "duration_months", "has_ppo",
        "hidden_score", "source", "url", "status", "posted_at", "first_seen_at", "last_seen_at",
        "raw_location",
    ),
    "startups": (
        "name", "domain", "website_url", "hq_city", "hq_state", "hidden_score",
        "open_internships", "funding_stage", "last_funded_at", "open_to_cold_pitch",
        "ats_provider", "ats_slug", "github_org", "tech_stack", "discovered_via", "last_checked_at",
    ),
    "leads": (
        "name", "domain", "source", "source_url", "hq_city", "hq_state", "sector", "founders",
        "funding_stage", "funding_amount", "last_funded_at", "batch", "is_hiring", "team_size",
        "linked_startup", "last_seen_at",
    ),
    "contacts": (
        "company", "company_domain", "hidden_score", "full_name", "role", "email", "profile_url",
        "source_method", "confidence_score", "verified_mx", "discovered_at",
    ),
}  # fmt: skip

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def build_rows(repo: Repository, what: ExportKind, flt: ExportFilter) -> list[dict[str, Any]]:
    """Plain-dict rows for one export kind, in ``COLUMNS[what]`` order."""
    if what == "opportunities":
        return [
            {
                "title": o.title,
                "company": s.name if s else None,
                "company_domain": s.canonical_domain if s else None,
                "city": o.city_canonical,
                "state": o.state,
                "work_mode": o.work_mode,
                "country": o.country,
                "stipend_inr_min": o.stipend_inr_min,
                "stipend_inr_max": o.stipend_inr_max,
                "is_paid": o.is_paid,
                "duration_months": o.duration_months,
                "has_ppo": o.has_ppo,
                "hidden_score": s.hidden_score if s else None,
                "source": o.source,
                "url": o.url,
                "status": o.status,
                "posted_at": o.posted_at,
                "first_seen_at": o.first_seen_at,
                "last_seen_at": o.last_seen_at,
                "raw_location": o.raw_location,
            }
            for o, s in repo.query_opportunities(flt)
        ]
    if what == "startups":
        open_counts = repo.open_opportunity_counts()
        return [
            {
                "name": s.name,
                "domain": s.canonical_domain,
                "website_url": s.website_url,
                "hq_city": s.hq_city_canonical,
                "hq_state": s.hq_state,
                "hidden_score": s.hidden_score,
                "open_internships": open_counts.get(s.id or -1, 0),
                "funding_stage": s.funding_stage,
                "last_funded_at": s.last_funded_at,
                "open_to_cold_pitch": s.open_to_cold_pitch,
                "ats_provider": s.ats_provider,
                "ats_slug": s.ats_slug,
                "github_org": s.github_org,
                "tech_stack": s.tech_stack,
                "discovered_via": s.discovered_via,
                "last_checked_at": s.last_checked_at,
            }
            for s in repo.query_startups(flt)
        ]
    if what == "leads":
        return [
            {
                "name": lead.name,
                "domain": lead.domain,
                "source": lead.source,
                "source_url": lead.source_url,
                "hq_city": lead.hq_city_canonical,
                "hq_state": lead.hq_state,
                "sector": lead.sector,
                "founders": lead.founders,
                "funding_stage": lead.funding_stage,
                "funding_amount": lead.funding_amount,
                "last_funded_at": lead.last_funded_at,
                "batch": lead.batch,
                "is_hiring": lead.is_hiring,
                "team_size": lead.team_size,
                "linked_startup": lead.startup_id is not None,
                "last_seen_at": lead.last_seen_at,
            }
            for lead in repo.query_leads(flt)
        ]
    if what == "contacts":
        return [
            {
                "company": s.name,
                "company_domain": s.canonical_domain,
                "hidden_score": s.hidden_score,
                "full_name": c.full_name,
                "role": c.role,
                "email": c.email,
                "profile_url": c.profile_url,
                "source_method": c.source_method,
                "confidence_score": c.confidence_score,
                "verified_mx": c.verified_mx,
                "discovered_at": c.discovered_at,
            }
            for c, s in repo.query_contacts(flt)
        ]
    raise ValueError(f"unknown export kind {what!r}; choose from {EXPORT_KINDS}")


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _csv_cell(value: Any) -> str:
    value = _plain(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        value = "; ".join(map(str, value))
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def rows_to_csv(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> str:
    """CSV text with a fixed header; cells are formula-safe (shared by CLI exports and the UI)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
    return buffer.getvalue()


def rows_to_json(rows: Sequence[dict[str, Any]]) -> str:
    """JSON array with enums/dates converted to plain values (lists and numbers kept)."""
    payload = [{k: _plain(v) for k, v in row.items()} for row in rows]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render(rows: Sequence[dict[str, Any]], what: ExportKind, fmt: ExportFormat) -> str:
    """Serialize rows as CSV (header always present) or a JSON array."""
    if fmt == "json":
        return rows_to_json(rows)
    if fmt != "csv":
        raise ValueError(f"unknown format {fmt!r}; choose csv or json")
    return rows_to_csv(rows, COLUMNS[what])


def write_export(text: str, out: Path) -> Path:
    """Write ``text`` to ``out`` (parent dirs created); returns the path."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def default_export_path(what: ExportKind, fmt: ExportFormat, *, today: date) -> Path:
    """``exports/<kind>_<YYYYMMDD>.<fmt>`` (``exports/`` is git-ignored)."""
    return Path("exports") / f"{what}_{today:%Y%m%d}.{fmt}"


def summarize_stats(repo: Repository, scoring: ScoringConfig) -> dict[str, Any]:
    """``repo.stats()`` plus hidden-score labels using ``scoring.yaml`` thresholds."""
    raw = repo.stats()
    th = scoring.thresholds
    labels = {"high": 0, "medium": 0, "low": 0, "unscored": 0}
    for score in raw.pop("startup_scores"):
        if score is None:
            labels["unscored"] += 1
        else:
            labels["high" if score >= th.high else "medium" if score >= th.medium else "low"] += 1
    raw["startups_by_score"] = labels
    leads_total = raw["counts"]["leads"]
    raw["leads_without_domain"] = leads_total - raw["leads_with_domain"]
    return raw
