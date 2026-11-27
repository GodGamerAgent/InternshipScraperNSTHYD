"""Guess, fetch and verify public ATS boards (ADR 007).

Used two ways:

* **Slug probing** during ``enrich`` (:func:`probe_startup`): for a startup with a known domain but
  no ATS, try a few conservative slugs (``acme.in`` → ``acme``) on the Greenhouse / Lever / Ashby /
  Workable public JSON APIs and keep the first board that passes the anti-collision check.
* **Search discovery** (``src/collectors/ats_search.py``): verify ``(provider, slug)`` pairs found
  via a search API and infer the company's own domain from the board's postings.

Anti-collision (a 200 OK alone proves nothing — ``acme`` may be someone else's board):
a board is accepted for a startup only if a link inside the board points at the startup's domain,
or the board's display name (Greenhouse ``company_name``, Workable ``name``) equals the startup's
name / domain label after removing legal suffixes and ``name_noise`` words (exact, never prefix).
Boards with neither signal are rejected. All requests are documented public APIs
(``respect_robots=False``, ADR 002) through ``HttpClient`` (pacing, retries, ETag cache).
"""

import html
import logging
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from src.core.http import FetchError, HttpClient
from src.core.text import normalize_text
from src.core.urls import canonical_domain, host_in
from src.models import lead_name_key

logger = logging.getLogger(__name__)

_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "keka.com",
    "zohorecruit.com", "zohorecruit.in", "freshteam.com", "darwinbox.in",
)  # fmt: skip
BOARD_URLS: dict[str, str] = {
    "greenhouse": "https://job-boards.greenhouse.io/{slug}",
    "lever": "https://jobs.lever.co/{slug}",
    "ashby": "https://jobs.ashbyhq.com/{slug}",
    "workable": "https://apply.workable.com/{slug}",
    "rippling": "https://ats.rippling.com/{slug}/jobs",
    "breezyhr": "https://{slug}.breezy.hr/",
    "recruitee": "https://{slug}.recruitee.com/",
}
# Documented APIs allowed to skip robots.txt (ADR 002). Every other provider respects it (ADR 009).
ROBOTS_OPT_OUT = frozenset({"greenhouse", "lever", "ashby", "workable"})
_MAX_STRINGS = 5000


class ProbeRules(Protocol):
    """Shape of ``sources.yaml: ats_probe`` (satisfied by ``collectors.config.AtsProbeConfig``)."""

    min_slug_length: int
    max_candidates: int
    strip_prefixes: Sequence[str]
    stopwords: Sequence[str]
    name_noise: Sequence[str]


# ---------------------------------------------------------------------------- naming helpers
def registrable_domain(domain: str) -> str:
    """``careers.acme.co.in`` → ``acme.co.in``; ``jobs.acme.io`` → ``acme.io``."""
    labels = domain.lower().split(".")
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def registrable_label(domain: str) -> str:
    """The brand label of a domain: ``acme.co.in`` → ``acme``."""
    return registrable_domain(domain).split(".")[0]


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def name_core(name: str, noise: Sequence[str] = ()) -> str:
    """Comparable company name: legal suffixes and noise words removed, alphanumerics only."""
    skip = {n.lower() for n in noise}
    words = [w for w in normalize_text(lead_name_key(name)).split() if w not in skip]
    return "".join(words)


def candidate_slugs(domain: str, name: str, rules: ProbeRules) -> list[str]:
    """Conservative board-slug guesses, most likely first.

    ``getacme.in`` / "Acme Labs Pvt Ltd" → ``["getacme", "acme", "acmelabs"]`` (capped by
    ``max_candidates``). Candidates shorter than ``min_slug_length`` or in ``stopwords`` are
    skipped: short generic slugs collide with unrelated companies' boards.
    """
    label = registrable_label(domain)
    raw = [label]
    raw += [
        label[len(p) :]
        for p in rules.strip_prefixes
        if label.startswith(p) and len(label) - len(p) >= rules.min_slug_length
    ]
    words = normalize_text(lead_name_key(name)).split() if name else []
    if words:
        raw += ["".join(words), "-".join(words)]
    stop = {w.lower() for w in rules.stopwords}
    out: list[str] = []
    for candidate in raw:
        slug = candidate.lower().strip("-")
        if (
            _SLUG_OK.match(slug)
            and len(slug.replace("-", "")) >= rules.min_slug_length
            and slug not in stop
            and slug not in out
        ):
            out.append(slug)
    return out[: rules.max_candidates]


# ---------------------------------------------------------------------------- board snapshots
@dataclass(frozen=True, slots=True)
class BoardSnapshot:
    """What a public board revealed about its owner (used for verification only)."""

    provider: str
    slug: str
    company_name: str | None
    job_count: int
    urls: tuple[str, ...]

    @property
    def board_url(self) -> str:
        return BOARD_URLS.get(self.provider, "").format(slug=self.slug)


def _strings(value: Any) -> Iterator[str]:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, Mapping):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def snapshot_from_payload(provider: str, slug: str, raw: Any) -> BoardSnapshot:
    """Provider-agnostic extraction: job count, display name, every URL in the payload."""
    if isinstance(raw, list):
        jobs = raw
    elif isinstance(raw, Mapping):
        jobs = raw.get("jobs") or raw.get("offers") or []  # Recruitee: {"offers": [...]}
    else:
        jobs = []
    name = (
        raw.get("name") if isinstance(raw, Mapping) and isinstance(raw.get("name"), str) else None
    )
    if name is None and jobs and isinstance(jobs[0], Mapping):
        company = jobs[0].get("company_name")  # Greenhouse, Recruitee
        if isinstance(jobs[0].get("company"), Mapping):  # BreezyHR: {"company": {"name": …}}
            company = jobs[0]["company"].get("name")
        name = company if isinstance(company, str) else None
    urls: list[str] = []
    for count, text in enumerate(_strings(raw)):
        if count >= _MAX_STRINGS:
            break
        urls.extend(_URL.findall(html.unescape(text)))
    return BoardSnapshot(
        provider=provider,
        slug=slug,
        company_name=name.strip() if name else None,
        job_count=len(jobs) if isinstance(jobs, list) else 0,
        urls=tuple(dict.fromkeys(u.rstrip(".,;") for u in urls)),
    )


async def fetch_board(
    http: HttpClient, provider: str, slug: str, endpoint: str
) -> BoardSnapshot | None:
    """GET a public board; ``None`` for 404/non-200, unknown Lever companies, or bad JSON."""
    url = endpoint.format(slug=quote(slug, safe=""))
    try:
        # Documented public job-board API: Tier 1 opts out of robots (ADR 002), others respect it
        # (ADR 009). Pacing/retries apply either way.
        response = await http.get(url, respect_robots=provider not in ROBOTS_OPT_OUT)
    except FetchError as exc:
        logger.info("ats_probe.fetch_failed", extra={"provider": provider, "slug": slug,
                                                     "status": exc.status})  # fmt: skip
        return None
    if response.status_code != 200:
        return None
    try:
        raw = response.json()
    except ValueError:
        return None
    if isinstance(raw, Mapping) and raw.get("ok") is False:  # Lever's "Document not found"
        return None
    return snapshot_from_payload(provider, slug, raw)


# ---------------------------------------------------------------------------- verification
def _domains(urls: Sequence[str]) -> Iterator[str]:
    for url in urls:
        try:
            yield canonical_domain(url)
        except ValueError:
            continue


def board_matches(
    snapshot: BoardSnapshot, *, domain: str, name: str, name_noise: Sequence[str] = ()
) -> bool:
    """Anti-collision: does this board plausibly belong to the startup at ``domain``?"""
    own = registrable_domain(domain)
    if any(registrable_domain(d) == own for d in _domains(snapshot.urls)):
        return True
    if snapshot.company_name:
        board = name_core(snapshot.company_name, name_noise)
        targets = {t for t in (name_core(name, name_noise), _alnum(registrable_label(domain))) if t}
        # Exact match only: a prefix rule would accept "Atlan Travel Group" for "Atlan".
        if board and board in targets:
            return True
    return False


def infer_domain(
    snapshot: BoardSnapshot, *, non_company_hosts: Sequence[str], name_noise: Sequence[str] = ()
) -> str | None:
    """The company's own domain for a search-discovered board, or ``None`` if not plausible.

    Most-linked domain in the postings (job boards, social sites and ATS hosts excluded) whose brand
    label matches the board slug or display name.
    """
    counts: Counter[str] = Counter()
    for domain in _domains(snapshot.urls):
        if host_in(domain, non_company_hosts) or host_in(domain, ATS_HOSTS):
            continue
        counts[registrable_domain(domain)] += 1
    slug = _alnum(snapshot.slug)
    board = name_core(snapshot.company_name, name_noise) if snapshot.company_name else ""
    for domain, _ in counts.most_common():
        label = _alnum(registrable_label(domain))
        if not label:
            continue
        if label in {slug, board} or (
            len(label) >= 4 and (label in slug or slug in label or (board and label in board))
        ):
            return domain
    return None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    provider: str
    slug: str
    snapshot: BoardSnapshot


async def probe_startup(
    http: HttpClient,
    *,
    domain: str,
    name: str,
    endpoints: Mapping[str, str],
    rules: ProbeRules,
    providers: Sequence[str],
) -> ProbeResult | None:
    """Try candidate slugs × providers; return the first board that passes ``board_matches``."""
    for slug in candidate_slugs(domain, name, rules):
        for provider in providers:
            snapshot = await fetch_board(http, provider, slug, endpoints[provider])
            if snapshot is None:
                continue
            if board_matches(snapshot, domain=domain, name=name, name_noise=rules.name_noise):
                logger.info("ats_probe.verified", extra={"domain": domain, "provider": provider,
                                                         "slug": slug})  # fmt: skip
                return ProbeResult(provider, slug, snapshot)
            logger.info(
                "ats_probe.collision_rejected",
                extra={"domain": domain, "provider": provider, "slug": slug,
                       "board_name": snapshot.company_name},
            )  # fmt: skip
    return None
