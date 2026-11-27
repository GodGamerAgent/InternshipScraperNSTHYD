"""SQLModel table definitions — a private persistence detail of ``src/storage``.

Domain code never imports these; it uses ``src.models`` and the ``Repository``. Enum values are
stored as plain strings, list fields as JSON, unknown cities as NULL, and datetimes as
aware UTC (SQLModel's ``UTCDateTime`` rejects naive values).
"""

from datetime import date, datetime

from sqlalchemy import JSON, Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, SQLModel


class StartupRow(SQLModel, table=True):
    """``startups`` — natural key ``canonical_domain``."""

    __tablename__ = "startups"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    canonical_domain: str = Field(unique=True, index=True)
    website_url: str | None = None
    hq_country: str | None = None
    hq_city_canonical: str | None = Field(default=None, index=True)
    hq_state: str | None = None
    hq_lat: float | None = None
    hq_lng: float | None = None
    hq_raw_location: str | None = None
    ats_provider: str | None = None
    ats_slug: str | None = None
    funding_stage: str | None = None
    last_funded_at: date | None = None
    github_org: str | None = None
    team_size: int | None = None
    tech_stack: list[str] | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))
    hidden_score: int | None = Field(default=None, index=True)
    open_to_cold_pitch: bool | None = None
    discovered_via: str | None = None
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class OpportunityRow(SQLModel, table=True):
    """``opportunities`` — natural key ``fingerprint`` (see ``opportunity_fingerprint``)."""

    __tablename__ = "opportunities"

    id: int | None = Field(default=None, primary_key=True)
    fingerprint: str = Field(unique=True, index=True)
    startup_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("startups.id", ondelete="CASCADE"), index=True),
    )
    source: str = Field(index=True)
    title: str
    url: str
    description: str | None = None
    raw_location: str | None = None
    country: str | None = None
    city_canonical: str | None = Field(default=None, index=True)
    state: str | None = None
    work_mode: str | None = Field(default=None, index=True)
    lat: float | None = None
    lng: float | None = None
    stipend_inr_min: int | None = None
    stipend_inr_max: int | None = None
    is_paid: bool | None = None
    duration_months: float | None = None
    has_ppo: bool | None = None
    is_hidden_lead: bool = False
    tech_stack: list[str] | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))
    posted_at: datetime | None = None
    status: str = "open"
    first_seen_at: datetime
    last_seen_at: datetime


class ContactRow(SQLModel, table=True):
    """``contacts`` — natural key ``(startup_id, dedupe_key)``."""

    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("startup_id", "dedupe_key", name="uq_contact_dedupe"),)

    id: int | None = Field(default=None, primary_key=True)
    startup_id: int = Field(
        sa_column=Column(
            Integer, ForeignKey("startups.id", ondelete="CASCADE"), nullable=False, index=True
        ),
    )
    dedupe_key: str
    full_name: str | None = None
    role: str | None = None
    email: str | None = Field(default=None, index=True)
    profile_url: str | None = None
    source_method: str
    confidence_score: float
    verified_mx: bool = False
    discovered_at: datetime
    created_at: datetime
    updated_at: datetime


class LeadRow(SQLModel, table=True):
    """``leads`` — natural key ``name_key`` (normalized name, see ``lead_name_key``)."""

    __tablename__ = "leads"

    id: int | None = Field(default=None, primary_key=True)
    name_key: str = Field(unique=True, index=True)
    name: str
    domain: str | None = Field(default=None, index=True)
    source: str = Field(index=True)
    source_url: str | None = None
    hq_country: str | None = None
    hq_city_canonical: str | None = Field(default=None, index=True)
    hq_state: str | None = None
    hq_lat: float | None = None
    hq_lng: float | None = None
    hq_raw_location: str | None = None
    sector: str | None = None
    founders: list[str] | None = Field(default=None, sa_column=Column(JSON(none_as_null=True)))
    funding_stage: str | None = None
    funding_amount: str | None = None
    last_funded_at: date | None = None
    # v0.2 (ADR 008): nullable, so ``Repository.create_schema`` can ALTER-add them to old files.
    batch: str | None = None
    is_hiring: bool | None = None
    team_size: int | None = None
    github_org: str | None = None
    startup_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("startups.id", ondelete="SET NULL"), index=True),
    )
    first_seen_at: datetime
    last_seen_at: datetime
