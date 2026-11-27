"""Closed vocabularies used across models, storage and the CLI.

City slugs are deliberately *not* an enum: cities are config-driven (``config/india_geo.yaml``), so
adding one must not require a code change. Only the two reserved slugs live here.
"""

from enum import StrEnum

CITY_UNKNOWN = "unknown"
CITY_REMOTE_INDIA = "remote_india"
RESERVED_CITY_SLUGS = frozenset({CITY_UNKNOWN, CITY_REMOTE_INDIA})


class Country(StrEnum):
    """Country bucket. ``GLOBAL`` = anything outside India (including global-remote roles)."""

    IN = "IN"
    GLOBAL = "GLOBAL"


class WorkMode(StrEnum):
    """Where the work happens. ``REMOTE_INDIA`` = IST-compatible / India-only remote."""

    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE_INDIA = "remote_india"
    REMOTE_GLOBAL = "remote_global"


class ResolutionMethod(StrEnum):
    """Which step of the geo pipeline produced a location (for debugging and confidence)."""

    ALIAS = "alias"
    NEIGHBORHOOD = "neighborhood"
    STATE_ALIAS = "state_alias"
    STATE = "state"
    REMOTE = "remote"
    PIN_CODE = "pin_code"
    HQ_TEXT_PIN = "hq_text_pin"
    HQ_CITY = "hq_city"
    SOURCE_DEFAULT = "source_default"
    FOREIGN = "foreign"
    UNRESOLVED = "unresolved"


class AtsProvider(StrEnum):
    """Applicant-tracking systems we can detect or poll."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKABLE = "workable"
    RIPPLING = "rippling"
    BREEZYHR = "breezyhr"
    RECRUITEE = "recruitee"
    SMARTRECRUITERS = (
        "smartrecruiters"  # detection only: its API's robots.txt disallows us (ADR 009)
    )
    KEKA = "keka"
    ZOHORECRUIT = "zohorecruit"
    FRESHTEAM = "freshteam"
    DARWINBOX = "darwinbox"
    OTHER = "other"


class FundingStage(StrEnum):
    """Most recent known funding stage."""

    BOOTSTRAPPED = "bootstrapped"
    PRE_SEED = "pre_seed"
    SEED = "seed"
    SERIES_A = "series_a"
    SERIES_B = "series_b"
    SERIES_C_PLUS = "series_c_plus"
    UNKNOWN = "unknown"


class OpportunityStatus(StrEnum):
    """Lifecycle of a posting or lead."""

    OPEN = "open"
    CLOSED = "closed"


class ContactSourceMethod(StrEnum):
    """How a contact was discovered (see ``config/scoring.yaml: contact_confidence``)."""

    DIRECT_PUBLIC = "direct_public"
    GITHUB_PUBLIC = "github_public"
    PATTERN_MX_VERIFIED = "pattern_mx_verified"
