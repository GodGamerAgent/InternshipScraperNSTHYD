"""Three-pass **public** contact discovery (``implementation.md`` §3E).

| Pass | Method | Confidence* | Source |
|------|--------|-------------|--------|
| 1 | ``direct_public`` | 0.95 | emails / profile links published on the startup's own pages |
| 2 | ``github_public`` | 0.85 | GitHub org email + public members' profiles |
| 3 | ``pattern_mx_verified`` | 0.50 | ``first@`` / ``first.last@`` from public founder names |

Pass 3 guesses are kept only if the domain has MX records.

\\* from ``config/scoring.yaml: contact_confidence``.

Compliance guardrails (``AGENTS.md`` §4):

* **DNS MX lookups only** (``dnspython``). There is no SMTP code here — no ``RCPT TO``, no
  connection to any mail server. A null MX (RFC 7505) or missing MX means "do not guess".
* Only emails on the **company's own domain** are kept; personal webmail (gmail, …) is never
  stored, even if published. Non-recruiting mailboxes (press@, sales@, …) are excluded.
* Profile URLs (LinkedIn, GitHub, X) are recorded as links only; those sites are never fetched.
"""

import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Protocol
from urllib.parse import unquote, urlsplit

import dns.asyncresolver
import dns.exception
from bs4 import BeautifulSoup, Tag

from src.core.http import FetchError, HttpClient
from src.core.text import html_to_text
from src.enrichment.github_signals import API as GITHUB_API
from src.enrichment.github_signals import github_headers
from src.enrichment.site import SitePage
from src.models import Contact, ContactSourceMethod

logger = logging.getLogger(__name__)

_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)+"
)
_PERSON_PATH = {
    "linkedin.com": re.compile(r"^/in/[^/]+/?$"),
    "github.com": re.compile(r"^/[A-Za-z0-9-]+/?$"),
    "x.com": re.compile(r"^/[A-Za-z0-9_]{1,15}/?$"),
    "twitter.com": re.compile(r"^/[A-Za-z0-9_]{1,15}/?$"),
}
_NAME = re.compile(r"^[A-Z][a-zA-Z'.-]+(?: [A-Z][a-zA-Z'.-]+){1,3}$")
_ROLE_HINT = re.compile(r"founder|ceo|cto|coo|chief|head|director|hr|talent|recruit|people", re.I)
_HONORIFIC = re.compile(r"^(?:dr|mr|mrs|ms|prof)\.?\s+", re.I)


# ---------------------------------------------------------------------------- DNS (MX only)
class MxResolver(Protocol):
    """Answers "does this domain accept mail?" — implemented with DNS MX lookups only."""

    async def has_mx(self, domain: str) -> bool: ...


class DnsMxResolver:
    """``dnspython`` MX lookup with a per-process cache. Never opens an SMTP connection."""

    def __init__(self, *, timeout_s: float = 5.0, resolver: object | None = None) -> None:
        self._resolver = resolver or dns.asyncresolver.Resolver()
        self._timeout = timeout_s
        self._cache: dict[str, bool] = {}

    async def has_mx(self, domain: str) -> bool:
        domain = domain.lower().rstrip(".")
        if domain not in self._cache:
            try:
                answer = await self._resolver.resolve(domain, "MX", lifetime=self._timeout)  # type: ignore[attr-defined]
                exchanges = [str(record.exchange).rstrip(".") for record in answer]
                self._cache[domain] = any(exchanges)  # RFC 7505 null MX "." -> no mail
            except dns.exception.DNSException as exc:
                logger.debug(
                    "contacts.mx_lookup_failed", extra={"domain": domain, "error": repr(exc)}
                )
                self._cache[domain] = False
        return self._cache[domain]


# ---------------------------------------------------------------------------- helpers
def _on_domain(email: str, domain: str) -> bool:
    host = email.rsplit("@", 1)[-1].lower()
    return host == domain or host.endswith("." + domain)


def _mailbox_role(local: str, hiring_prefixes: Iterable[str]) -> str | None:
    base = re.split(r"[.+_-]", local.lower())[0]
    return local.capitalize() if base in {p.lower() for p in hiring_prefixes} else None


def _person_name(anchor: Tag) -> str | None:
    for candidate in (anchor.get("aria-label") or "", anchor.get_text(" ", strip=True)):
        candidate = re.sub(
            r"\s+on\s+(?:linkedin|github|x|twitter)\s*$", "", str(candidate), flags=re.I
        )
        if _NAME.match(candidate.strip()):
            return candidate.strip()
    container = anchor.find_parent(["div", "li", "article", "section"])
    heading = container.find(["h2", "h3", "h4", "h5", "strong"]) if container else None
    if heading and _NAME.match(heading.get_text(" ", strip=True)):
        return heading.get_text(" ", strip=True)
    return None


def _person_role(anchor: Tag) -> str | None:
    container = anchor.find_parent(["div", "li", "article"])
    if container is None:
        return None
    for node in container.find_all(["p", "span", "small"]):
        text = node.get_text(" ", strip=True)
        if text and len(text) <= 80 and _ROLE_HINT.search(text):
            return text
    return None


def split_name(full_name: str) -> tuple[str, str | None] | None:
    """``"Dr. Asha  Rao"`` -> ``("asha", "rao")``; ASCII-folded; ``None`` if unusable."""
    cleaned = _HONORIFIC.sub("", " ".join(full_name.split()))
    ascii_name = unicodedata.normalize("NFKD", cleaned).encode("ascii", "ignore").decode()
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in ascii_name.split()]
    parts = [p for p in parts if len(p) > 1]
    if not parts:
        return None
    return parts[0], (parts[-1] if len(parts) > 1 else None)


def dedupe(contacts: Iterable[Contact]) -> list[Contact]:
    """Keep the most confident contact per ``dedupe_key`` (first seen wins ties)."""
    best: dict[str, Contact] = {}
    for contact in contacts:
        current = best.get(contact.dedupe_key)
        if current is None or contact.confidence_score > current.confidence_score:
            best[contact.dedupe_key] = contact
    return list(best.values())


# ---------------------------------------------------------------------------- Pass 1
def extract_direct_contacts(
    pages: Sequence[SitePage],
    *,
    startup_id: int,
    domain: str,
    hiring_prefixes: Sequence[str],
    excluded_prefixes: Sequence[str] = (),
    profile_hosts: Sequence[str] = (),
    confidence: float = 0.95,
    exclude_profiles: Iterable[str] = (),
) -> list[Contact]:
    """Pass 1: company-domain emails and personal profile links on the startup's own pages.

    ``exclude_profiles`` holds profile URLs that are not people (e.g. the company's GitHub org).
    """
    excluded = {p.lower() for p in excluded_prefixes}
    skip_profiles = {u.rstrip("/").lower() for u in exclude_profiles}
    contacts: list[Contact] = []
    for page in pages:
        if not page.ok:
            continue
        soup = BeautifulSoup(page.html, "lxml")
        emails: dict[str, None] = {}
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if href.lower().startswith("mailto:"):
                emails.setdefault(unquote(href[7:]).split("?", 1)[0].strip().lower(), None)
        for match in _EMAIL.finditer(html_to_text(page.html)):
            emails.setdefault(match.group().lower().rstrip("."), None)
        for email in emails:
            local = email.split("@", 1)[0]
            if not _on_domain(email, domain) or re.split(r"[.+_-]", local)[0] in excluded:
                continue
            try:
                contacts.append(
                    Contact(
                        startup_id=startup_id,
                        email=email,
                        role=_mailbox_role(local, hiring_prefixes),
                        source_method=ContactSourceMethod.DIRECT_PUBLIC,
                        confidence_score=confidence,
                    )
                )
            except ValueError:
                continue  # not a valid address after all
        for anchor in soup.find_all("a", href=True):
            parts = urlsplit(anchor["href"])
            host = (parts.hostname or "").lower().removeprefix("www.")
            pattern = _PERSON_PATH.get(host)
            if host not in profile_hosts or pattern is None or not pattern.match(parts.path):
                continue
            url = f"https://{host}{parts.path.rstrip('/')}"
            if url.lower() in skip_profiles:
                continue
            contacts.append(
                Contact(
                    startup_id=startup_id,
                    profile_url=url,
                    full_name=_person_name(anchor),
                    role=_person_role(anchor),
                    source_method=ContactSourceMethod.DIRECT_PUBLIC,
                    confidence_score=confidence,
                )
            )
    return dedupe(contacts)


# ---------------------------------------------------------------------------- Pass 2
async def github_contacts(
    http: HttpClient,
    org: str,
    *,
    startup_id: int,
    domain: str,
    org_email: str | None = None,
    token: str | None = None,
    max_members: int = 5,
    confidence: float = 0.85,
) -> list[Contact]:
    """Pass 2: the org's public email and up to ``max_members`` public members' profiles.

    Members' emails are kept only when they are on the company domain.
    """
    contacts: list[Contact] = []
    if org_email and _on_domain(org_email, domain):
        contacts.append(
            Contact(
                startup_id=startup_id,
                email=org_email,
                role="GitHub org contact",
                source_method=ContactSourceMethod.GITHUB_PUBLIC,
                confidence_score=confidence,
            )
        )
    if max_members <= 0:
        return contacts
    headers = github_headers(token)
    try:
        # Documented REST API -> robots opt-out (ADR 002).
        members = await http.get_json(
            f"{GITHUB_API}/orgs/{org}/public_members",
            params={"per_page": max_members},
            headers=headers,
            respect_robots=False,
        )
    except FetchError as exc:
        logger.info("contacts.github_members_failed", extra={"org": org, "status": exc.status})
        return contacts
    users = [m for m in members if isinstance(m, dict) and m.get("type") == "User"]
    for member in users[:max_members]:
        try:
            user = await http.get_json(
                f"{GITHUB_API}/users/{member['login']}", headers=headers, respect_robots=False
            )
        except FetchError:
            user = member
        email = user.get("email")
        contacts.append(
            Contact(
                startup_id=startup_id,
                profile_url=user.get("html_url") or f"https://github.com/{member['login']}",
                full_name=user.get("name") or None,
                email=email if email and _on_domain(email, domain) else None,
                role="GitHub org member",
                source_method=ContactSourceMethod.GITHUB_PUBLIC,
                confidence_score=confidence,
            )
        )
    return dedupe(contacts)


# ---------------------------------------------------------------------------- Pass 3
async def pattern_contacts(
    founders: Sequence[str],
    *,
    startup_id: int,
    domain: str,
    templates: Sequence[str],
    resolver: MxResolver,
    confidence: float = 0.50,
) -> list[Contact]:
    """Pass 3: ``first@`` / ``first.last@`` guesses for public founder names.

    Generated only when the domain has MX records (DNS lookup; no SMTP probing ever), and marked
    ``verified_mx=True`` — which means "the domain receives mail", not "this mailbox exists".
    """
    names = [(name, split_name(name)) for name in founders]
    names = [(name, parts) for name, parts in names if parts is not None]
    if not names or not templates or not await resolver.has_mx(domain):
        return []
    contacts: list[Contact] = []
    for full_name, (first, last) in names:  # type: ignore[misc]
        for template in templates:
            if "{last}" in template and last is None:
                continue
            local = template.format(first=first, last=last or "")
            contacts.append(
                Contact(
                    startup_id=startup_id,
                    full_name=full_name,
                    role="Founder",
                    email=f"{local}@{domain}",
                    source_method=ContactSourceMethod.PATTERN_MX_VERIFIED,
                    confidence_score=confidence,
                    verified_mx=True,
                )
            )
    return dedupe(contacts)
