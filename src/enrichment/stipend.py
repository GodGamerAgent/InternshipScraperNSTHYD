"""Extract Indian internship terms from free text: monthly INR stipend, duration, PPO.

Handles the conventions in ``context.md`` §4:

* ``₹15,000/month`` · ``INR 20,000 per month`` · ``Rs. 25000 pm`` · ``15k - 25k/mo`` ·
  ``₹15K–25K per month`` · ``Stipend: 30,000`` · ``up to ₹40,000`` · ``1 lakh/month``
* ``unpaid`` / ``no stipend`` → ``is_paid=False``; ``competitive stipend`` / ``paid internship`` →
  ``is_paid=True`` without an amount.
* Durations ``2 months`` · ``6-month`` · ``3 to 6 months`` (upper bound kept) · ``12 weeks``,
  ignoring experience requirements (``0-6 months of experience``).
* PPO: ``PPO`` · ``pre-placement offer`` · ``full-time conversion``; ``no PPO`` → ``False``.

Deliberately conservative: annual figures (``LPA``, ``per annum``, ``CTC`` — usually the PPO
package), foreign currencies, and bare numbers without a stipend/currency/monthly cue are ignored.
This module is the parser grammar, not business configuration, so its patterns live in code.
"""

import re
from dataclasses import dataclass

MIN_MONTHLY_INR = 1_000
MAX_MONTHLY_INR = 500_000

_NUM = r"\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?"
_UNIT = r"k\b|thousand\b|lakhs?\b|lacs?\b"
_CUR = r"₹|rs\.?|inr\b|rupees?\b"
_AMOUNT = re.compile(
    rf"(?P<cur1>{_CUR})?\s*(?P<n1>{_NUM})\s*(?P<u1>{_UNIT})?"
    rf"(?:\s*(?:-|–|—|to)\s*(?P<cur2>{_CUR})?\s*(?P<n2>{_NUM})\s*(?P<u2>{_UNIT})?)?",
    re.IGNORECASE,
)
_MONTHLY_AFTER = re.compile(
    r"^\s*(?:/\s*(?:month|mo|m)\b|per\s+month|a\s+month|p\.\s?m\.?|pm\b|monthly|per\s+mensem)",
    re.IGNORECASE,
)
_ANNUAL_AFTER = re.compile(
    r"^\s*(?:lpa\b|/\s*(?:year|yr|annum|a)\b|per\s+(?:year|annum)|p\.\s?a\.?|pa\b|annually|ctc\b|"
    r"cr\b|crores?\b)",
    re.IGNORECASE,
)
_DURATION_AFTER = re.compile(
    r"^\s*[- ]?(?:months?|mos?|weeks?|wks?|days?|years?|yrs?|hours?|hrs?)\b", re.IGNORECASE
)
# Non-INR currencies seen on Indian startups' global boards (explicit list: a generic "any 3
# letters" rule would also swallow words like "for 2000").
_FOREIGN_CODES = (
    r"usd|eur|gbp|sgd|aed|myr|idr|php|thb|vnd|jpy|cny|hkd|aud|cad|nzd|chf|sar|egp|rm|"
    r"dollars?|euros?"
)
_FOREIGN_BEFORE = re.compile(
    rf"(?:[$€£¥]|(?:us|s|a|c)\$|\b(?:{_FOREIGN_CODES}))\s*$", re.IGNORECASE
)
_FOREIGN_AFTER = re.compile(rf"^\s*(?:{_FOREIGN_CODES})\b", re.IGNORECASE)
_STIPEND_WORD = re.compile(r"\bstipends?\b", re.IGNORECASE)
_UP_TO_BEFORE = re.compile(r"\b(?:up\s*to|upto|max(?:imum)?)\s*$", re.IGNORECASE)

_UNPAID = re.compile(
    r"\bunpaid\b|\bno\s+stipend\b|\bwithout\s+(?:any\s+)?stipend\b|"
    r"\bstipend\s*[:\-]?\s*(?:nil|none|n/?a|not\s+applicable)\b|\bvolunteer\s+(?:role|position)\b",
    re.IGNORECASE,
)
_PAID_HINT = re.compile(
    r"\bpaid\s+intern(?:ship)?s?\b|\b(?:competitive|attractive|fixed|monthly|"
    r"performance[- ]based)\s+stipend\b|"
    r"\bstipend\s+(?:provided|offered|will\s+be\s+(?:provided|paid))\b",
    re.IGNORECASE,
)

_DURATION = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?:(?:-|–|to)\s*(?P<b>\d+(?:\.\d+)?)\s*)?[- ]?"
    r"(?P<unit>months?|mos?\b|weeks?|wks?\b)",
    re.IGNORECASE,
)
_DURATION_NOISE_BEFORE = re.compile(
    r"(?:experience|exp\.?|notice|probation|within|after|every|last|past|over\s+the)[^.\n]{0,20}$",
    re.IGNORECASE,
)
_DURATION_NOISE_AFTER = re.compile(
    r"^[^.\n]{0,15}\b(?:experience|exp\b|notice|probation|ago|old)\b", re.IGNORECASE
)
_DURATION_CONTEXT = re.compile(r"\b(?:duration|internship|tenure|period)\b", re.IGNORECASE)

_PPO_NEGATIVE = re.compile(
    r"\bno\s+(?:ppo|pre[- ]?placement\s+offer)|\bppo\s+(?:is\s+)?not\s+(?:available|offered)|"
    r"\bdoes\s+not\s+(?:include|offer)\s+(?:a\s+)?(?:ppo|pre[- ]?placement)",
    re.IGNORECASE,
)
_PPO_POSITIVE = re.compile(
    r"\bppos?\b|\bpre[- ]?placement\s+offers?\b|\b(?:full[- ]time|ft)\s+(?:conversion|offer)\b|"
    r"\bconver(?:t|sion)\s+(?:to|into)\s+(?:a\s+)?full[- ]time\b|\breturn\s+offers?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Stipend:
    """Monthly stipend in INR. ``None`` fields mean unknown."""

    min_inr: int | None = None
    max_inr: int | None = None
    is_paid: bool | None = None

    @property
    def has_amount(self) -> bool:
        return self.min_inr is not None or self.max_inr is not None


@dataclass(frozen=True, slots=True)
class InternshipTerms:
    """Everything :func:`extract_internship_terms` found."""

    stipend: Stipend
    duration_months: float | None
    has_ppo: bool | None


# ---------------------------------------------------------------------------- stipend
def _to_inr(number: str, unit: str | None) -> float:
    value = float(number.replace(",", ""))
    unit = (unit or "").lower()
    if unit in {"k", "thousand"}:
        value *= 1_000
    elif unit.startswith(("lakh", "lac")):
        value *= 100_000
    return value


def _in_bounds(value: float) -> bool:
    return MIN_MONTHLY_INR <= value <= MAX_MONTHLY_INR


def parse_stipend(text: str | None) -> Stipend:
    """Find the monthly INR stipend in ``text``; prefer amounts next to the word "stipend"."""
    if not text:
        return Stipend()
    best: tuple[bool, int, int | None, int] | None = None  # (stipend_ctx, -pos, min, max)
    for match in _AMOUNT.finditer(text):
        start, end = match.span()
        before, after = text[max(0, start - 40) : start], text[end : end + 30]
        if _FOREIGN_BEFORE.search(before) or _FOREIGN_AFTER.match(after):
            continue
        if _ANNUAL_AFTER.match(after) or _DURATION_AFTER.match(after):
            continue
        monthly = bool(_MONTHLY_AFTER.match(after))
        stipend_ctx = bool(_STIPEND_WORD.search(before) or _STIPEND_WORD.match(after.strip()))
        currency = bool(match["cur1"] or match["cur2"])
        if not (monthly or stipend_ctx or currency):
            continue

        n1, n2 = match["n1"], match["n2"]
        u1, u2 = match["u1"], match["u2"]
        hi_value = _to_inr(n2, u2) if n2 else None
        # "15-25k": the unit written once applies to both ends.
        lo_value = _to_inr(n1, u1 or (u2 if n2 and float(n1.replace(",", "")) < 1000 else None))
        values = [v for v in (lo_value, hi_value) if v is not None]
        if not all(_in_bounds(v) for v in values):
            continue
        lo, hi = int(min(values)), int(max(values))
        if hi_value is None and _UP_TO_BEFORE.search(before):
            lo_out: int | None = None
        else:
            lo_out = lo
        candidate = (stipend_ctx, -start, lo_out, hi)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is not None:
        return Stipend(best[2], best[3], is_paid=True)
    if _UNPAID.search(text):
        return Stipend(is_paid=False)
    if _PAID_HINT.search(text):
        return Stipend(is_paid=True)
    return Stipend()


def stipend_from_structured(
    minimum: float | None,
    maximum: float | None,
    currency: str | None,
    interval: str | None,
) -> Stipend | None:
    """Stipend from an ATS compensation object, or ``None`` if it is not a monthly INR figure.

    ``interval`` accepts Lever (``per-month-salary``) and Ashby (``1 MONTH``) spellings. Zeros
    (Lever's "not disclosed") are treated as missing.
    """
    if (currency or "").upper() != "INR" or "month" not in (interval or "").lower():
        return None
    values = [int(v) for v in (minimum, maximum) if v]
    if not values or not all(_in_bounds(v) for v in values):
        return None
    return Stipend(min(values), max(values), is_paid=True)


# ---------------------------------------------------------------------------- duration
def parse_duration_months(text: str | None) -> float | None:
    """Internship length in months (upper bound of a range), ignoring experience requirements."""
    if not text:
        return None
    best: tuple[bool, int, float] | None = None  # (has_context, -pos, months)
    for match in _DURATION.finditer(text):
        start, end = match.span()
        before, after = text[max(0, start - 40) : start], text[end : end + 30]
        if _DURATION_NOISE_BEFORE.search(before) or _DURATION_NOISE_AFTER.match(after):
            continue
        value = max(float(match["a"]), float(match["b"] or 0))
        if match["unit"].lower().startswith(("week", "wk")):
            value = round(value / 4.345, 1)
        if not 0.5 <= value <= 24:
            continue
        candidate = (bool(_DURATION_CONTEXT.search(before + after)), -start, value)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return best[2] if best else None


# ---------------------------------------------------------------------------- PPO
def detect_ppo(text: str | None) -> bool | None:
    """``True`` if a PPO / full-time conversion is mentioned, ``False`` if ruled out, else None."""
    if not text:
        return None
    if _PPO_NEGATIVE.search(text):
        return False
    if _PPO_POSITIVE.search(text):
        return True
    return None


def extract_internship_terms(text: str | None) -> InternshipTerms:
    """Stipend, duration and PPO in one pass (pass ``title + description``)."""
    return InternshipTerms(
        stipend=parse_stipend(text),
        duration_months=parse_duration_months(text),
        has_ppo=detect_ppo(text),
    )
