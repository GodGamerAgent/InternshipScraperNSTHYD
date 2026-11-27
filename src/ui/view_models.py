"""Pure view models for the NiceGUI dashboard: filters → table rows, labels and download payloads.

No NiceGUI import here and no I/O beyond read-only ``Repository`` queries, so everything is
unit-tested offline (``tests/test_ui_helpers.py``). Data access goes only through Phase 5's
``ExportFilter`` + ``Repository.query_*`` / ``stats()``; serialization reuses ``src/exports.py``
(so CSV cells stay formula-safe).
"""

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from src.enrichment.geo import GeoResolver
from src.enrichment.scorer import Thresholds
from src.exports import rows_to_csv, rows_to_json
from src.models import (
    CITY_REMOTE_INDIA,
    CITY_UNKNOWN,
    Contact,
    ContactSourceMethod,
    Opportunity,
    Startup,
    WorkMode,
)
from src.storage import ExportFilter, Repository

DownloadFormat = Literal["csv", "json"]

WORK_MODE_LABELS: dict[str, str] = {
    WorkMode.ONSITE.value: "On-site",
    WorkMode.HYBRID.value: "Hybrid",
    WorkMode.REMOTE_INDIA.value: "Remote (India)",
    WorkMode.REMOTE_GLOBAL.value: "Remote (global)",
}
METHOD_LABELS: dict[str, str] = {
    ContactSourceMethod.DIRECT_PUBLIC.value: "Published on company site",
    ContactSourceMethod.GITHUB_PUBLIC.value: "Public GitHub",
    ContactSourceMethod.PATTERN_MX_VERIFIED.value: "Pattern guess",
}
_METHOD_RANK = {
    ContactSourceMethod.DIRECT_PUBLIC: 3,
    ContactSourceMethod.GITHUB_PUBLIC: 2,
    ContactSourceMethod.PATTERN_MX_VERIFIED: 1,
}
UNCONFIRMED_NOTE = "Unconfirmed guess: the domain accepts mail, but this mailbox was never verified"


def _col(name: str, label: str, *, align: str = "left", sortable: bool = True) -> dict[str, Any]:
    return {"name": name, "label": label, "field": name, "align": align, "sortable": sortable}


# Column specs in the shape ``ui.table(columns=...)`` expects; ``name`` doubles as the row key.
TABLE_COLUMNS: dict[str, list[dict[str, Any]]] = {
    "internships": [
        _col("startup", "Startup"),
        _col("role", "Role"),
        _col("city", "City"),
        _col("work_mode", "Work mode"),
        _col("stipend", "Monthly stipend (INR)", align="right"),
        _col("duration", "Duration"),
        _col("ppo", "PPO"),
        _col("apply_url", "Apply", sortable=False),
        _col("contact", "Public contact", sortable=False),
    ],
    "hidden": [
        _col("rank", "#", align="right"),
        _col("startup", "Startup"),
        _col("score", "Score", align="right"),
        _col("label", "Band"),
        _col("city", "HQ"),
        _col("funding", "Funding"),
        _col("cold_pitch", "Cold pitch"),
        _col("github_url", "GitHub", sortable=False),
        _col("open_internships", "Open roles", align="right"),
        _col("contacts", "Public contacts", sortable=False),
    ],
    "contacts": [
        _col("startup", "Startup"),
        _col("name", "Name"),
        _col("role", "Role"),
        _col("contact", "Email / profile", sortable=False),
        _col("method", "Source"),
        _col("confidence", "Confidence", align="right"),
        _col("verified_mx", "MX verified"),
        _col("note", "Status"),
    ],
}


# ---------------------------------------------------------------------------- filters
@dataclass(frozen=True, slots=True)
class UiFilters:
    """Sidebar state. Empty collections mean "no restriction"; score 0 means "no minimum"."""

    cities: tuple[str, ...] = ()
    include_remote: bool = False
    work_modes: tuple[str, ...] = ()
    min_hidden_score: int = 0
    paid_only: bool = False
    ppo_only: bool = False
    keyword: str = ""

    def to_export_filter(self) -> ExportFilter:
        """The Phase 5 ``ExportFilter`` for the repository query (open postings only)."""
        return ExportFilter(
            cities=frozenset(self.cities) or None,
            work_modes=frozenset(WorkMode(m) for m in self.work_modes) or None,
            include_remote=self.include_remote,
            min_hidden_score=self.min_hidden_score or None,
        )

    def matches_keyword(self, *texts: Any) -> bool:
        """Case-insensitive "every word appears somewhere" match over the given fields."""
        words = self.keyword.lower().split()
        if not words:
            return True
        haystack = " ".join(str(t) for t in texts if t).lower()
        return all(word in haystack for word in words)


# ---------------------------------------------------------------------------- formatting
def format_inr(amount: int) -> str:
    """Indian digit grouping: ``150000`` → ``₹1,50,000``."""
    digits = str(abs(int(amount)))
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "₹" + ",".join([*groups, tail])


def stipend_label(minimum: int | None, maximum: int | None, is_paid: bool | None) -> str:
    """``₹15,000–₹25,000/mo``, ``up to ₹40,000/mo``, ``Unpaid``, ``Paid (amount not listed)``."""
    if minimum and maximum and minimum != maximum:
        return f"{format_inr(minimum)}–{format_inr(maximum)}/mo"
    if maximum and not minimum:
        return f"up to {format_inr(maximum)}/mo"
    if minimum or maximum:
        return f"{format_inr(minimum or maximum or 0)}/mo"
    if is_paid is False:
        return "Unpaid"
    if is_paid:
        return "Paid (amount not listed)"
    return "—"


def duration_label(months: float | None) -> str:
    """``6 months``, ``1 month``, ``2.8 months`` or ``—``."""
    if months is None:
        return "—"
    return "1 month" if months == 1 else f"{months:g} months"


def ppo_label(has_ppo: bool | None) -> str:
    return {True: "PPO", False: "No PPO"}.get(has_ppo, "—")  # type: ignore[arg-type]


def yes_no(value: bool | None) -> str:
    return {True: "Yes", False: "No"}.get(value, "—")  # type: ignore[arg-type]


def city_labels(geo: GeoResolver) -> dict[str, str]:
    """``slug → display name`` for every configured city plus the reserved slugs."""
    labels = {slug: geo.city(slug).display_name for slug in geo.city_slugs}
    return labels | {CITY_REMOTE_INDIA: "Remote (India)", CITY_UNKNOWN: "—"}


def score_band(score: int | None, thresholds: Thresholds) -> str:
    if score is None:
        return "unscored"
    return "high" if score >= thresholds.high else "medium" if score >= thresholds.medium else "low"


def contact_display(contact: Contact) -> str:
    """Email (or profile URL), with pattern guesses explicitly marked unconfirmed."""
    shown = contact.email or contact.profile_url or ""
    if contact.source_method is ContactSourceMethod.PATTERN_MX_VERIFIED:
        return f"{shown} (unconfirmed guess)"
    return shown


def contact_note(contact: Contact) -> str:
    if contact.source_method is ContactSourceMethod.PATTERN_MX_VERIFIED:
        return UNCONFIRMED_NOTE
    if contact.source_method is ContactSourceMethod.GITHUB_PUBLIC:
        return "Listed on the company's public GitHub"
    return "Published on the company's own website"


def _contact_rank(contact: Contact) -> tuple[bool, int, float]:
    return (bool(contact.email), _METHOD_RANK[contact.source_method], contact.confidence_score)


def contacts_by_startup(repo: Repository) -> dict[int, list[Contact]]:
    """All public contacts grouped per startup, best first (email, method, confidence)."""
    grouped: dict[int, list[Contact]] = defaultdict(list)
    for contact, _startup in repo.query_contacts(ExportFilter()):
        grouped[contact.startup_id].append(contact)
    return {sid: sorted(cs, key=_contact_rank, reverse=True) for sid, cs in grouped.items()}


# ---------------------------------------------------------------------------- KPIs
@dataclass(frozen=True, slots=True)
class KpiCard:
    label: str
    value: int
    icon: str


def kpi_cards(stats: Mapping[str, Any]) -> list[KpiCard]:
    """Header cards from ``Repository.stats()``."""
    counts = stats.get("counts", {})
    open_roles = stats.get("opportunities_by_status", {}).get("open", 0)
    return [
        KpiCard("Startups", int(counts.get("startups", 0)), "business"),
        KpiCard("Open opportunities", int(open_roles), "work"),
        KpiCard("Hidden leads", int(counts.get("leads", 0)), "travel_explore"),
        KpiCard("Public contacts", int(counts.get("contacts", 0)), "contact_mail"),
    ]


# ---------------------------------------------------------------------------- rows
def internship_rows(
    repo: Repository, filters: UiFilters, *, labels: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Open internships matching the sidebar, best hidden score first."""
    contacts = contacts_by_startup(repo)
    rows: list[dict[str, Any]] = []
    for opp, startup in repo.query_opportunities(filters.to_export_filter()):
        if filters.paid_only and opp.is_paid is not True:
            continue
        if filters.ppo_only and opp.has_ppo is not True:
            continue
        city = labels.get(opp.city_canonical, opp.city_canonical)
        company = startup.name if startup else "—"
        if not filters.matches_keyword(opp.title, company, city, opp.description, opp.source):
            continue
        best = contacts.get(startup.id or -1, []) if startup else []
        rows.append(_internship_row(opp, company, city, best[0] if best else None))
    return rows


def _internship_row(
    opp: Opportunity, company: str, city: str, contact: Contact | None
) -> dict[str, Any]:
    return {
        "id": opp.id,
        "startup": company,
        "role": opp.title,
        "city": city,
        "work_mode": WORK_MODE_LABELS.get(str(opp.work_mode), "—") if opp.work_mode else "—",
        "stipend": stipend_label(opp.stipend_inr_min, opp.stipend_inr_max, opp.is_paid),
        "duration": duration_label(opp.duration_months),
        "ppo": ppo_label(opp.has_ppo),
        "apply_url": opp.url,
        "contact": contact_display(contact) if contact else "—",
        "source": opp.source,
    }


def hidden_opportunity_rows(
    repo: Repository,
    filters: UiFilters,
    *,
    labels: Mapping[str, str],
    thresholds: Thresholds,
    max_contacts: int = 3,
) -> list[dict[str, Any]]:
    """Scored startups, highest ``hidden_score`` first (paid/PPO toggles don't apply here)."""
    contacts = contacts_by_startup(repo)
    open_counts = repo.open_opportunity_counts()
    scored = [
        s for s in repo.query_startups(filters.to_export_filter()) if s.hidden_score is not None
    ]
    scored.sort(key=lambda s: (-(s.hidden_score or 0), s.name.lower()))
    rows: list[dict[str, Any]] = []
    for startup in scored:
        city = labels.get(startup.hq_city_canonical, startup.hq_city_canonical)
        if not filters.matches_keyword(
            startup.name, startup.canonical_domain, city, " ".join(startup.tech_stack)
        ):
            continue
        top = contacts.get(startup.id or -1, [])[:max_contacts]
        rows.append(_hidden_row(len(rows) + 1, startup, city, thresholds, open_counts, top))
    return rows


def _hidden_row(
    rank: int,
    startup: Startup,
    city: str,
    thresholds: Thresholds,
    open_counts: Mapping[int, int],
    top: Sequence[Contact],
) -> dict[str, Any]:
    stage = str(startup.funding_stage).replace("_", " ").title() if startup.funding_stage else ""
    funded = startup.last_funded_at.isoformat() if startup.last_funded_at else ""
    return {
        "id": startup.id,
        "rank": rank,
        "startup": startup.name,
        "website": startup.website_url or f"https://{startup.canonical_domain}",
        "score": startup.hidden_score,
        "label": score_band(startup.hidden_score, thresholds),
        "city": city,
        "funding": " · ".join(p for p in (stage, funded) if p) or "—",
        "cold_pitch": yes_no(startup.open_to_cold_pitch),
        "github_url": f"https://github.com/{startup.github_org}" if startup.github_org else "",
        "open_internships": open_counts.get(startup.id or -1, 0),
        "contacts": "; ".join(contact_display(c) for c in top) or "—",
    }


def contact_rows(
    repo: Repository,
    filters: UiFilters,
    *,
    methods: Collection[str] = (),
    verified: bool | None = None,
) -> list[dict[str, Any]]:
    """Public contacts of startups matching the sidebar, filtered by method / MX status."""
    rows: list[dict[str, Any]] = []
    for contact, startup in repo.query_contacts(filters.to_export_filter()):
        method = contact.source_method.value
        if methods and method not in methods:
            continue
        if verified is not None and contact.verified_mx is not verified:
            continue
        if not filters.matches_keyword(
            startup.name, contact.full_name, contact.role, contact.email, contact.profile_url
        ):
            continue
        rows.append(
            {
                "id": contact.id,
                "startup": startup.name,
                "name": contact.full_name or "—",
                "role": contact.role or "—",
                "contact": contact_display(contact),
                "method": METHOD_LABELS[method],
                "confidence": f"{contact.confidence_score:.2f}",
                "verified_mx": yes_no(contact.verified_mx),
                "note": contact_note(contact),
                "is_guess": contact.source_method is ContactSourceMethod.PATTERN_MX_VERIFIED,
            }
        )
    return rows


# ---------------------------------------------------------------------------- downloads
@dataclass(frozen=True, slots=True)
class DownloadPayload:
    """What ``ui.download.content`` needs."""

    content: bytes
    filename: str
    media_type: str
    rows: int = 0
    columns: list[str] = field(default_factory=list)


def download_payload(
    rows: Sequence[dict[str, Any]],
    *,
    table: str,
    fmt: DownloadFormat,
    today: date,
) -> DownloadPayload:
    """CSV (formula-safe, fixed header) or JSON of the rows currently shown in a tab."""
    columns = [c["field"] for c in TABLE_COLUMNS[table]]
    if fmt == "csv":
        text = rows_to_csv(rows, columns)
        media = "text/csv"
    elif fmt == "json":
        text = rows_to_json([{c: row.get(c) for c in columns} for row in rows])
        media = "application/json"
    else:
        raise ValueError(f"unknown format {fmt!r}; choose csv or json")
    return DownloadPayload(
        content=text.encode("utf-8"),
        filename=f"{table}_{today:%Y%m%d}.{fmt}",
        media_type=media,
        rows=len(rows),
        columns=columns,
    )
