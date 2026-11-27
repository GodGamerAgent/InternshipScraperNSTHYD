"""Pydantic v2 data contracts. These are the only shapes that cross layer boundaries."""

from src.models.collected import CollectedOpportunity
from src.models.contact import Contact
from src.models.enums import (
    CITY_REMOTE_INDIA,
    CITY_UNKNOWN,
    AtsProvider,
    ContactSourceMethod,
    Country,
    FundingStage,
    OpportunityStatus,
    ResolutionMethod,
    WorkMode,
)
from src.models.lead import StartupLead, lead_name_key
from src.models.location import ResolvedLocation
from src.models.opportunity import Opportunity, opportunity_fingerprint
from src.models.startup import Startup

__all__ = [
    "CITY_REMOTE_INDIA",
    "CITY_UNKNOWN",
    "AtsProvider",
    "CollectedOpportunity",
    "Contact",
    "ContactSourceMethod",
    "Country",
    "FundingStage",
    "Opportunity",
    "OpportunityStatus",
    "ResolutionMethod",
    "ResolvedLocation",
    "Startup",
    "StartupLead",
    "WorkMode",
    "lead_name_key",
    "opportunity_fingerprint",
]
