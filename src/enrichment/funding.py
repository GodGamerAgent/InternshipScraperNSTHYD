"""Parse Indian funding-news headlines into structured events.

Handles headline styles seen on Entrackr / Inc42 (2026-11)::

    GIVA raises Rs 90 Cr from BlackSoil for retail expansion
    Green Aero raises $14 Mn led by Crane Ventures
    Bengaluru-based fintech startup Finnable secures ₹405 Cr
    Exclusive: ExampleLabs bags $1.2 Mn in pre-seed round

Roundups ("Funding Galore: Indian Startups Raised $200 Mn This Week"), multi-company headlines and
non-funding news return ``None``. Stage and HQ city are read from the title plus the summary
("Bengaluru-based aerospace startup … has raised $14 million in a Series A").
"""

import re
from dataclasses import dataclass

from src.models import FundingStage

_AMOUNT = (
    r"(?:Rs\.?|INR|₹|US\$|\$|USD)\s*\d[\d,]*(?:\.\d+)?\s*"
    r"(?:Crores?|Cr|Mn|Million|Bn|Billion|Lakhs?|K)?"
)
_VERBS = (
    r"raises|raised|secures|secured|bags|bagged|nets|netted|picks\s+up|gets|closes|lands|"
    r"mops\s+up|garners|receives|snags|scoops\s+up"
)
_HEADLINE = re.compile(
    rf"^(?P<company>.+?)\s+(?:{_VERBS})\s+(?:(?:over|around|about|nearly|up\s+to)\s+)?"
    rf"(?P<amount>{_AMOUNT})",
    re.IGNORECASE,
)
_PREFIX = re.compile(r"^(?:exclusive|breaking|update|report|funding\s+alert)\s*[:|\-–]\s*", re.I)
_DESCRIPTOR_WORD = re.compile(
    r"\b(?:startup|platform|company|firm|brand|maker|player|provider|marketplace|unicorn)\b", re.I
)
_REJECT = re.compile(
    r"[,;]|\b(?:and|startups|funding|week|weekly|roundup|these|indian|investors?)\b", re.I
)
_BASED = re.compile(r"\b(?P<city>[A-Z][A-Za-z]+(?:[ -][A-Z][A-Za-z]+)?)-based\b")
_STAGES: tuple[tuple[re.Pattern[str], FundingStage], ...] = (
    (re.compile(r"\bpre[- ]?seed\b", re.I), FundingStage.PRE_SEED),
    (re.compile(r"\bpre[- ]?series[- ]?a\b", re.I), FundingStage.SEED),  # bucketed with seed
    (re.compile(r"\bseed\b", re.I), FundingStage.SEED),
    (re.compile(r"\bseries[- ]?a\b", re.I), FundingStage.SERIES_A),
    (re.compile(r"\bseries[- ]?b\b", re.I), FundingStage.SERIES_B),
    (re.compile(r"\bseries[- ]?[c-h]\b", re.I), FundingStage.SERIES_C_PLUS),
)


@dataclass(frozen=True, slots=True)
class FundingEvent:
    """One startup's funding announcement."""

    company: str
    amount: str
    stage: FundingStage | None
    city: str | None  # raw "X-based" city text, resolve with GeoResolver


def _clean_company(raw: str) -> str | None:
    company = _PREFIX.sub("", raw.strip()).strip(" '\"‘’“”")
    descriptors = list(_DESCRIPTOR_WORD.finditer(company))
    if descriptors:  # "Bengaluru-based fintech startup Finnable" -> "Finnable"
        company = company[descriptors[-1].end() :].strip()
    if not company or _REJECT.search(company) or len(company.split()) > 6:
        return None
    if not company[0].isupper() and not company[0].isdigit():
        return None
    return company


def detect_stage(text: str) -> FundingStage | None:
    """The stage mentioned *first* in ``text`` (the current round; later mentions are history).

    On a tie at the same position the more specific pattern wins (``pre-seed`` over ``seed``).
    """
    best: tuple[int, int, FundingStage] | None = None
    for priority, (pattern, stage) in enumerate(_STAGES):
        match = pattern.search(text)
        if match and (best is None or (match.start(), priority) < best[:2]):
            best = (match.start(), priority, stage)
    return best[2] if best else None


def parse_funding_headline(title: str, summary: str = "") -> FundingEvent | None:
    """Structured event for a single-company funding headline, else ``None``."""
    match = _HEADLINE.match(" ".join(title.split()))
    if not match:
        return None
    company = _clean_company(match["company"])
    if company is None:
        return None
    context = f"{title}\n{summary}"
    city = _BASED.search(context)
    return FundingEvent(
        company=company,
        amount=" ".join(match["amount"].split()),
        stage=detect_stage(context),
        city=city["city"] if city else None,
    )
