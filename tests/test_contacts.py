"""Offline tests for 3-pass public contact discovery (DNS and HTTP mocked)."""

import json
import re

import dns.resolver
import httpx
import pytest

from src.collectors.config import SourcesConfig
from src.core.config import PROJECT_ROOT
from src.enrichment.contacts import (
    DnsMxResolver,
    dedupe,
    extract_direct_contacts,
    github_contacts,
    pattern_contacts,
    split_name,
)
from src.enrichment.site import SitePage
from src.models import ContactSourceMethod
from tests.conftest import CONFIG_DIR, FIXTURES, FakeTime, make_client

DOMAIN = "examplestack.example"


@pytest.fixture(scope="module")
def sources() -> SourcesConfig:
    return SourcesConfig.load(CONFIG_DIR)


def pages() -> list[SitePage]:
    out = []
    for path, name in (("/", "home.html"), ("/careers", "careers.html"), ("/about", "about.html")):
        url = f"https://{DOMAIN}{path}"
        out.append(SitePage(path, url, url, 200, (FIXTURES / "site" / name).read_text()))
    return out


# ---------------------------------------------------------------- Pass 1: direct_public
def test_direct_contacts_from_own_pages(sources: SourcesConfig) -> None:
    contacts = extract_direct_contacts(
        pages(),
        startup_id=1,
        domain=DOMAIN,
        hiring_prefixes=sources.contacts.hiring_mailbox_prefixes,
        excluded_prefixes=sources.contacts.excluded_mailbox_prefixes,
        profile_hosts=sources.contacts.profile_hosts,
        exclude_profiles=["https://github.com/examplestack-oss"],  # the org, not a person
    )
    emails = {c.email: c.role for c in contacts if c.email}
    assert emails == {
        "hello@examplestack.example": "Hello",
        "careers@examplestack.example": "Careers",
        "founders@examplestack.example": "Founders",
    }
    # press@ (not recruiting), gmail (personal), agency (other domain), icon@2x.png: all dropped.
    profiles = {c.profile_url: (c.full_name, c.role) for c in contacts if c.profile_url}
    assert profiles == {
        "https://linkedin.com/in/asha-rao-example": ("Asha Rao", "Co-founder & CEO"),
        "https://github.com/vikram-iyer-example": ("Vikram Iyer", "Co-founder & CTO"),
        "https://x.com/vikram_example": ("Vikram Iyer", "Co-founder & CTO"),
    }
    assert all(c.source_method is ContactSourceMethod.DIRECT_PUBLIC for c in contacts)
    assert all(c.confidence_score == 0.95 for c in contacts)


def test_direct_contacts_skip_failed_pages(sources: SourcesConfig) -> None:
    blocked = SitePage("/team", "https://x.example/team", "https://x.example/team", None,
                       error="robots_disallowed")  # fmt: skip
    assert (
        extract_direct_contacts(
            [blocked], startup_id=1, domain="x.example", hiring_prefixes=["careers"]
        )
        == []
    )


# ---------------------------------------------------------------- Pass 2: github_public
def gh(name: str):
    return json.loads((FIXTURES / "github" / name).read_text(encoding="utf-8"))


async def test_github_contacts(fake_time: FakeTime) -> None:
    users = gh("users.json")
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/public_members"):
            return httpx.Response(200, json=gh("members.json"))
        login = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=users[login])

    async with make_client(handler, fake_time) as http:
        contacts = await github_contacts(
            http,
            "examplestack-oss",
            startup_id=1,
            domain=DOMAIN,
            org_email="oss@examplestack.example",
            max_members=5,
        )
    by_key = {c.email or c.profile_url: c for c in contacts}
    assert set(by_key) == {
        "oss@examplestack.example",
        "vikram@examplestack.example",  # member email on the company domain
        "https://github.com/meera-dev-example",  # gmail dropped, profile kept
    }
    assert by_key["https://github.com/meera-dev-example"].email is None
    assert by_key["vikram@examplestack.example"].full_name == "Vikram Iyer"
    assert all(c.source_method is ContactSourceMethod.GITHUB_PUBLIC for c in contacts)
    assert "/users/oss-bot-example" not in paths  # bots are not people
    assert "/robots.txt" not in paths


async def test_github_contacts_survive_api_errors(fake_time: FakeTime) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "rate limited"})

    async with make_client(handler, fake_time) as http:
        contacts = await github_contacts(
            http, "examplestack-oss", startup_id=1, domain=DOMAIN, org_email="oss@gmail.com"
        )
    assert contacts == []  # off-domain org email dropped; member lookup failure tolerated


# ---------------------------------------------------------------- Pass 3: pattern_mx_verified
class FakeMx:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.queries: list[str] = []

    async def has_mx(self, domain: str) -> bool:
        self.queries.append(domain)
        return self.answer


@pytest.mark.parametrize(
    ("name", "parts"),
    [
        ("Asha Rao", ("asha", "rao")),
        ("Dr. Ünal  Kaya", ("unal", "kaya")),
        ("Ravi Kumar Sharma", ("ravi", "sharma")),
        ("Madonna", ("madonna", None)),
        ("  ", None),
    ],
)
def test_split_name(name: str, parts) -> None:
    assert split_name(name) == parts


async def test_pattern_contacts_require_mx() -> None:
    resolver = FakeMx(True)
    contacts = await pattern_contacts(
        ["Asha Rao", "Madonna"],
        startup_id=1,
        domain=DOMAIN,
        templates=["{first}", "{first}.{last}"],
        resolver=resolver,
    )
    assert sorted(c.email for c in contacts) == [
        "asha.rao@examplestack.example",
        "asha@examplestack.example",
        "madonna@examplestack.example",  # no last name -> no first.last pattern
    ]
    assert all(c.verified_mx and c.confidence_score == 0.5 for c in contacts)
    assert all(c.source_method is ContactSourceMethod.PATTERN_MX_VERIFIED for c in contacts)
    assert resolver.queries == [DOMAIN]

    no_mail = await pattern_contacts(
        ["Asha Rao"], startup_id=1, domain=DOMAIN, templates=["{first}"], resolver=FakeMx(False)
    )
    assert no_mail == []
    skipped = FakeMx(True)
    assert await pattern_contacts([], startup_id=1, domain=DOMAIN, templates=["{first}"],
                                  resolver=skipped) == []  # fmt: skip
    assert skipped.queries == []  # no founders -> no DNS query at all


# ---------------------------------------------------------------- DNS resolver (MX only)
class _Record:
    def __init__(self, exchange: str) -> None:
        self.exchange = exchange


class FakeDnsResolver:
    """Stands in for ``dns.asyncresolver.Resolver`` — no network."""

    def __init__(self, zones: dict[str, list[str]]) -> None:
        self.zones = zones
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, name: str, rdtype: str, lifetime: float | None = None):
        self.calls.append((name, rdtype))
        if name not in self.zones:
            raise dns.resolver.NXDOMAIN()
        return [_Record(x) for x in self.zones[name]]


async def test_dns_mx_resolver() -> None:
    fake = FakeDnsResolver({"examplestack.example": ["mx1.mail.example."], "nullmx.example": ["."]})
    resolver = DnsMxResolver(resolver=fake)
    assert await resolver.has_mx("ExampleStack.example.") is True
    assert await resolver.has_mx("examplestack.example") is True  # cached
    assert await resolver.has_mx("nullmx.example") is False  # RFC 7505 null MX
    assert await resolver.has_mx("missing.example") is False  # NXDOMAIN
    assert fake.calls == [
        ("examplestack.example", "MX"),
        ("nullmx.example", "MX"),
        ("missing.example", "MX"),
    ]
    assert {rdtype for _, rdtype in fake.calls} == {"MX"}  # never anything but MX


def test_no_smtp_anywhere_in_source() -> None:
    """Guardrail: the codebase must never talk SMTP (AGENTS.md §4)."""
    offenders = [
        str(path)
        for path in (PROJECT_ROOT / "src").rglob("*.py")
        if re.search(r"^\s*(?:import|from)\s+(?:smtplib|aiosmtplib)", path.read_text(), re.M)
    ]
    assert offenders == []


def test_dedupe_keeps_highest_confidence() -> None:
    from src.models import Contact

    low = Contact(startup_id=1, email="a@x.example", source_method="github_public",
                  confidence_score=0.85)  # fmt: skip
    high = Contact(startup_id=1, email="A@x.example", source_method="direct_public",
                   confidence_score=0.95)  # fmt: skip
    (kept,) = dedupe([low, high])
    assert kept.source_method is ContactSourceMethod.DIRECT_PUBLIC
