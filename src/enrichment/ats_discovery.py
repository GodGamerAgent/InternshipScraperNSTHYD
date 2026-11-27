"""Discover ATS boards, cold-pitch invitations and GitHub orgs on a startup's own pages.

Pure analysis of already-fetched pages (``site.fetch_site_pages``):

* **ATS embeds** — links, iframes, scripts or redirects to Greenhouse / Lever / Ashby / Workable
  (Tier 1: the startup can be auto-enrolled for polling) and to India-popular HR portals
  (Keka, Zoho Recruit, Freshteam, Darwinbox; recorded for Tier 2/3 parsing). Only providers
  listed in ``sources.yaml: ats_embed_hosts`` are reported.
* **Cold-pitch invitations** — ``cold_pitch_phrases`` ("drop your resume", "general
  application", …) in the visible text of *hiring* pages (careers/jobs/join…) →
  ``open_to_cold_pitch``. Direct founder/recruiting emails are added by the scorer from contacts.
* **GitHub orgs** — ``github.com/<org>`` links, for ``github_signals`` and contacts Pass 2.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from src.core.text import html_to_text, normalize_text
from src.enrichment.site import SitePage

_SLUG = r"(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]*)"
# URL grammar per provider (most specific first). Hosts are cross-checked against config.
_ATS_PATTERNS: dict[str, tuple[str, ...]] = {
    "greenhouse": (
        rf"greenhouse\.io/embed/job_board(?:/js)?\?for={_SLUG}",
        rf"boards-api\.greenhouse\.io/v1/boards/{_SLUG}",
        rf"(?:job-)?boards\.greenhouse\.io/{_SLUG}",
    ),
    "lever": (rf"api\.lever\.co/v0/postings/{_SLUG}", rf"jobs\.lever\.co/{_SLUG}"),
    "ashby": (rf"api\.ashbyhq\.com/posting-api/job-board/{_SLUG}", rf"jobs\.ashbyhq\.com/{_SLUG}"),
    "workable": (
        rf"apply\.workable\.com/api/v1/widget/accounts/{_SLUG}",
        rf"apply\.workable\.com/{_SLUG}",
    ),
    "rippling": (
        rf"api\.rippling\.com/platform/api/ats/v1/board/{_SLUG}",
        rf"ats\.rippling\.com/{_SLUG}/jobs",
    ),
    "breezyhr": (rf"//{_SLUG}\.breezy\.hr",),
    "recruitee": (rf"//{_SLUG}\.recruitee\.com",),
    "smartrecruiters": (
        rf"(?:jobs|careers)\.smartrecruiters\.com/{_SLUG}",
        rf"api\.smartrecruiters\.com/v1/companies/{_SLUG}",
    ),
    "keka": (rf"//{_SLUG}\.keka\.com",),
    # Zoho's slug is the full portal host: the data-centre TLD (.in / .com) differs per tenant.
    "zohorecruit": (r"//(?P<slug>[A-Za-z0-9][A-Za-z0-9-]*\.zohorecruit\.(?:in|com))\b",),
    "freshteam": (rf"//{_SLUG}\.freshteam\.com",),
    "darwinbox": (rf"//{_SLUG}\.darwinbox\.in",),
}
_STOP_SLUGS = frozenset(
    {"embed", "js", "v1", "api", "jobs", "j", "www", "app", "careers", "static", "assets", "cdn"}
)
_GITHUB = re.compile(
    r"github\.com/(?:orgs/)?(?P<org>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?=[/\"'?#\s]|$)"
)
_GITHUB_RESERVED = frozenset(
    {"features", "about", "pricing", "login", "join", "topics", "sponsors", "marketplace",
     "enterprise", "explore", "settings", "apps", "orgs", "site", "security", "collections"}
)  # fmt: skip
# Public JSON job-board APIs (Tier 1 + ADR 009 Rippling / BreezyHR / Recruitee).
TIER1_PROVIDERS = frozenset(
    {"greenhouse", "lever", "ashby", "workable", "rippling", "breezyhr", "recruitee"}
)
# Providers a collector polls (Tier 1 JSON APIs + Zoho Recruit's HTML portal, ADR 008). Keka,
# Freshteam, Darwinbox and SmartRecruiters are only recorded (Keka's job data and SmartRecruiters'
# API are robots-disallowed).
POLLED_PROVIDERS = TIER1_PROVIDERS | {"zohorecruit"}
# Cold-pitch phrases only count on hiring pages: "write to us" in a homepage footer is not an
# invitation to pitch for an internship.
_HIRING_PATH = re.compile(r"career|job|join|work-with|hiring|intern", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AtsMatch:
    """An ATS board referenced from the startup's site."""

    provider: str
    slug: str
    evidence_url: str

    @property
    def tier1(self) -> bool:
        """True for a public JSON job-board API (verifiable by ``ats_probe``)."""
        return self.provider in TIER1_PROVIDERS

    @property
    def pollable(self) -> bool:
        """True if a collector polls this provider (auto-enrollment preference)."""
        return self.provider in POLLED_PROVIDERS


@dataclass(frozen=True, slots=True)
class SiteDiscovery:
    """Everything learned from one startup's pages."""

    ats: tuple[AtsMatch, ...] = ()
    open_to_cold_pitch: bool = False
    cold_pitch_evidence: tuple[str, ...] = ()
    github_orgs: tuple[str, ...] = ()
    pages_ok: int = 0
    js_only_pages: tuple[str, ...] = ()
    blocked_pages: tuple[str, ...] = ()

    @property
    def primary_tier1(self) -> AtsMatch | None:
        """The first pollable board, used to auto-enroll the startup (``ats_provider/slug``)."""
        return next((m for m in self.ats if m.tier1), None) or next(
            (m for m in self.ats if m.pollable), None
        )


def find_ats(markup: str, *, enabled_providers: Iterable[str]) -> list[tuple[str, str]]:
    """``(provider, slug)`` pairs referenced anywhere in ``markup`` (deduplicated, in order)."""
    enabled = set(enabled_providers)
    found: dict[tuple[str, str], None] = {}
    for provider, patterns in _ATS_PATTERNS.items():
        if provider not in enabled:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, markup, re.IGNORECASE):
                slug = match["slug"].rstrip(".").lower()
                if slug and slug not in _STOP_SLUGS:
                    found.setdefault((provider, slug), None)
    return list(found)


def find_github_orgs(markup: str) -> list[str]:
    """GitHub org/user logins linked from ``markup`` (reserved paths excluded)."""
    orgs: dict[str, None] = {}
    for match in _GITHUB.finditer(markup):
        org = match["org"]
        if org.lower() not in _GITHUB_RESERVED:
            orgs.setdefault(org, None)
    return list(orgs)


def analyze_site(
    pages: Sequence[SitePage],
    *,
    cold_pitch_phrases: Sequence[str],
    ats_embed_hosts: Mapping[str, Sequence[str]],
) -> SiteDiscovery:
    """Combine ATS / cold-pitch / GitHub signals from fetched pages."""
    phrases = {normalize_text(p): p for p in cold_pitch_phrases if p.strip()}
    ats: dict[tuple[str, str], AtsMatch] = {}
    evidence: dict[str, None] = {}
    orgs: dict[str, None] = {}
    for page in pages:
        haystacks = [page.final_url] if page.final_url != page.url else []
        if page.ok:
            haystacks.append(page.html)
            if _HIRING_PATH.search(page.path) or _HIRING_PATH.search(page.final_url):
                visible = f" {normalize_text(html_to_text(page.html))} "
                for key, phrase in phrases.items():
                    if f" {key} " in visible:
                        evidence.setdefault(phrase, None)
        for haystack in haystacks:
            for provider, slug in find_ats(haystack, enabled_providers=ats_embed_hosts):
                ats.setdefault((provider, slug), AtsMatch(provider, slug, page.url))
            for org in find_github_orgs(haystack):
                orgs.setdefault(org, None)
    return SiteDiscovery(
        ats=tuple(ats.values()),
        open_to_cold_pitch=bool(evidence),
        cold_pitch_evidence=tuple(evidence),
        github_orgs=tuple(orgs),
        pages_ok=sum(p.ok for p in pages),
        js_only_pages=tuple(p.path for p in pages if p.js_only and not p.rendered),
        blocked_pages=tuple(p.path for p in pages if p.error == "robots_disallowed"),
    )
