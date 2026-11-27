"""``Repository`` — the only code in the project that reads or writes SQLite.

Every write is an idempotent upsert on a natural key (see ``docs/ARCHITECTURE.md`` §4):

* new non-null values overwrite, ``None`` never erases existing data (``COALESCE``);
* creation timestamps (``created_at``, ``first_seen_at``, ``discovered_at``) are insert-only;
* contacts keep the discovery method/confidence of the *most* confident sighting, and
  ``verified_mx`` is sticky once true.
"""

import logging
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel
from sqlalchemy import (
    ColumnElement,
    Connection,
    Engine,
    Table,
    and_,
    case,
    exists,
    func,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import SQLModel

from src.core.urls import canonical_domain
from src.models import (
    CITY_UNKNOWN,
    Contact,
    Opportunity,
    OpportunityStatus,
    Startup,
    StartupLead,
    WorkMode,
    lead_name_key,
)
from src.models.base import utc_now
from src.storage.db import create_db_engine
from src.storage.filters import ExportFilter
from src.storage.tables import ContactRow, LeadRow, OpportunityRow, StartupRow

STARTUPS: Table = StartupRow.__table__  # type: ignore[assignment]
OPPORTUNITIES: Table = OpportunityRow.__table__  # type: ignore[assignment]
CONTACTS: Table = ContactRow.__table__  # type: ignore[assignment]
LEADS: Table = LeadRow.__table__  # type: ignore[assignment]

_CITY_FIELDS = frozenset({"hq_city_canonical", "city_canonical"})

logger = logging.getLogger(__name__)


def _to_db_value(key: str, value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC)  # SQLModel requires aware datetimes; always UTC
    if isinstance(value, list):
        return value or None  # [] -> NULL so COALESCE keeps an existing list
    if key in _CITY_FIELDS and value == CITY_UNKNOWN:
        return None  # "unknown" must never overwrite a resolved city
    return value


def _to_row(model: BaseModel, exclude: set[str] | None = None) -> dict[str, Any]:
    data = model.model_dump(exclude={"id", *(exclude or set())})
    return {key: _to_db_value(key, value) for key, value in data.items()}


def _to_model[M: BaseModel](model_cls: type[M], row: Mapping[str, Any]) -> M:
    return model_cls.model_validate({k: row[k] for k in model_cls.model_fields if k in row})


def _upsert(
    conn: Connection,
    table: Table,
    values: dict[str, Any],
    conflict_cols: list[str],
    *,
    insert_only: frozenset[str] = frozenset(),
    overwrite: frozenset[str] = frozenset(),
    custom: Mapping[str, Any] | None = None,
) -> int:
    """``INSERT … ON CONFLICT(conflict_cols) DO UPDATE`` returning the row id."""
    stmt = sqlite_insert(table).values(**values)
    excluded = stmt.excluded
    custom = custom or {}
    set_: dict[str, Any] = {}
    for col in values:
        if col in conflict_cols or col in insert_only:
            continue
        if col in custom:
            set_[col] = custom[col]
        elif col in overwrite:
            set_[col] = excluded[col]
        else:
            set_[col] = func.coalesce(excluded[col], table.c[col])
    stmt = stmt.on_conflict_do_update(index_elements=conflict_cols, set_=set_)
    return conn.execute(stmt.returning(table.c.id)).scalar_one()


class Repository:
    """SQLite-backed store for startups, opportunities and contacts.

    Usage::

        with Repository.open("data/scraper.db") as repo:
            sid = repo.upsert_startup(Startup.from_url("Acme", "https://acme.in"))
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def open(cls, db_path: Path | str) -> Self:
        """Open (creating if needed) the database at ``db_path`` (or ``":memory:"``)."""
        repo = cls(create_db_engine(db_path))
        repo.create_schema()
        return repo

    def create_schema(self) -> None:
        """Create all tables and indexes if they do not exist (safe to call repeatedly).

        Databases created by an older version get any *nullable* columns added since then via
        ``ALTER TABLE … ADD COLUMN`` (additive only: nothing is renamed or dropped, ADR 008).
        """
        SQLModel.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        with self.engine.begin() as conn:
            for table in SQLModel.metadata.sorted_tables:
                existing = {
                    row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')
                }
                for column in table.columns:
                    if column.name in existing:
                        continue
                    if not column.nullable or column.primary_key or column.unique:
                        raise RuntimeError(
                            f"{table.name}.{column.name} cannot be added in place; see ADR 008"
                        )
                    ddl = column.type.compile(dialect=self.engine.dialect)
                    conn.exec_driver_sql(
                        f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'
                    )
                    logger.info(
                        "storage.column_added", extra={"table": table.name, "column": column.name}
                    )

    def close(self) -> None:
        """Dispose of the connection pool."""
        self.engine.dispose()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------ startups
    def upsert_startup(self, startup: Startup) -> int:
        """Insert or merge a startup on ``canonical_domain``; returns its id."""
        with self.engine.begin() as conn:
            return self._upsert_startup(conn, startup)

    def upsert_startups(self, startups: Iterable[Startup]) -> list[int]:
        """Batch :meth:`upsert_startup` in one transaction."""
        with self.engine.begin() as conn:
            return [self._upsert_startup(conn, s) for s in startups]

    def _upsert_startup(self, conn: Connection, startup: Startup) -> int:
        now = utc_now()
        values = _to_row(startup) | {"created_at": now, "updated_at": now}
        return _upsert(
            conn,
            STARTUPS,
            values,
            ["canonical_domain"],
            insert_only=frozenset({"created_at"}),
            overwrite=frozenset({"updated_at"}),
        )

    def get_startup(self, startup_id: int) -> Startup | None:
        """Fetch a startup by id."""
        return self._get_one(Startup, STARTUPS, STARTUPS.c.id == startup_id)

    def get_startup_by_domain(self, domain_or_url: str) -> Startup | None:
        """Fetch a startup by domain or any URL on its site."""
        domain = canonical_domain(domain_or_url)
        return self._get_one(Startup, STARTUPS, STARTUPS.c.canonical_domain == domain)

    def list_startups(self) -> list[Startup]:
        """All startups ordered by id."""
        return self._list(Startup, STARTUPS)

    # ------------------------------------------------------------------ opportunities
    def upsert_opportunity(self, opportunity: Opportunity) -> int:
        """Insert or merge an opportunity on its fingerprint; returns its id.

        The fingerprint uses the owning startup's ``canonical_domain`` when ``startup_id`` is set,
        otherwise the domain of the posting URL.

        Raises:
            LookupError: If ``startup_id`` refers to a startup that does not exist.
        """
        with self.engine.begin() as conn:
            return self._upsert_opportunity(conn, opportunity)

    def upsert_opportunities(self, opportunities: Iterable[Opportunity]) -> list[int]:
        """Batch :meth:`upsert_opportunity` in one transaction."""
        with self.engine.begin() as conn:
            return [self._upsert_opportunity(conn, o) for o in opportunities]

    def _upsert_opportunity(self, conn: Connection, opportunity: Opportunity) -> int:
        if opportunity.startup_id is not None:
            domain = conn.execute(
                select(STARTUPS.c.canonical_domain).where(STARTUPS.c.id == opportunity.startup_id)
            ).scalar_one_or_none()
            if domain is None:
                raise LookupError(f"startup_id={opportunity.startup_id} does not exist")
        else:
            domain = canonical_domain(opportunity.url)
        now = utc_now()
        values = _to_row(opportunity, exclude={"first_seen_at", "last_seen_at"}) | {
            "fingerprint": opportunity.fingerprint(domain),
            "first_seen_at": now,
            "last_seen_at": now,
        }
        return _upsert(
            conn,
            OPPORTUNITIES,
            values,
            ["fingerprint"],
            insert_only=frozenset({"first_seen_at"}),
            overwrite=frozenset({"last_seen_at", "is_hidden_lead", "status"}),
        )

    def get_opportunity(self, opportunity_id: int) -> Opportunity | None:
        """Fetch an opportunity by id."""
        return self._get_one(Opportunity, OPPORTUNITIES, OPPORTUNITIES.c.id == opportunity_id)

    def list_opportunities(self, startup_id: int | None = None) -> list[Opportunity]:
        """All opportunities (optionally for one startup) ordered by id.

        City / work-mode / score filtering for exports arrives with the Phase 5 CLI.
        """
        where = OPPORTUNITIES.c.startup_id == startup_id if startup_id is not None else None
        return self._list(Opportunity, OPPORTUNITIES, where)

    # ------------------------------------------------------------------ contacts
    def upsert_contact(self, contact: Contact) -> int:
        """Insert or merge a contact on ``(startup_id, dedupe_key)``; returns its id."""
        with self.engine.begin() as conn:
            return self._upsert_contact(conn, contact)

    def upsert_contacts(self, contacts: Iterable[Contact]) -> list[int]:
        """Batch :meth:`upsert_contact` in one transaction."""
        with self.engine.begin() as conn:
            return [self._upsert_contact(conn, c) for c in contacts]

    def _upsert_contact(self, conn: Connection, contact: Contact) -> int:
        now = utc_now()
        values = _to_row(contact) | {
            "dedupe_key": contact.dedupe_key,
            "created_at": now,
            "updated_at": now,
        }
        stmt_excluded = sqlite_insert(CONTACTS).excluded
        more_confident = stmt_excluded.confidence_score >= CONTACTS.c.confidence_score
        custom = {
            "source_method": case(
                (more_confident, stmt_excluded.source_method), else_=CONTACTS.c.source_method
            ),
            "confidence_score": func.max(
                stmt_excluded.confidence_score, CONTACTS.c.confidence_score
            ),
            "verified_mx": func.max(stmt_excluded.verified_mx, CONTACTS.c.verified_mx),
        }
        return _upsert(
            conn,
            CONTACTS,
            values,
            ["startup_id", "dedupe_key"],
            insert_only=frozenset({"created_at", "discovered_at"}),
            overwrite=frozenset({"updated_at"}),
            custom=custom,
        )

    def list_contacts(self, startup_id: int | None = None) -> list[Contact]:
        """All contacts (optionally for one startup) ordered by id."""
        where = CONTACTS.c.startup_id == startup_id if startup_id is not None else None
        return self._list(Contact, CONTACTS, where)

    # ------------------------------------------------------------------ leads
    def upsert_lead(self, lead: StartupLead) -> int:
        """Insert or merge a lead on its normalized name; returns its id."""
        with self.engine.begin() as conn:
            return self._upsert_lead(conn, lead)

    def upsert_leads(self, leads: Iterable[StartupLead]) -> list[int]:
        """Batch :meth:`upsert_lead` in one transaction."""
        with self.engine.begin() as conn:
            return [self._upsert_lead(conn, lead) for lead in leads]

    def _upsert_lead(self, conn: Connection, lead: StartupLead) -> int:
        now = utc_now()
        values = _to_row(lead, exclude={"first_seen_at", "last_seen_at"}) | {
            "name_key": lead.name_key,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        return _upsert(
            conn,
            LEADS,
            values,
            ["name_key"],
            insert_only=frozenset({"first_seen_at"}),
            overwrite=frozenset({"last_seen_at"}),
        )

    def get_lead_by_name(self, name: str) -> StartupLead | None:
        """Fetch a lead by (any spelling of) its company name."""
        return self._get_one(StartupLead, LEADS, LEADS.c.name_key == lead_name_key(name))

    def list_leads(self, startup_id: int | None = None) -> list[StartupLead]:
        """All leads (optionally only those linked to one startup) ordered by id."""
        where = LEADS.c.startup_id == startup_id if startup_id is not None else None
        return self._list(StartupLead, LEADS, where)

    # ------------------------------------------------------------------ stats
    def counts(self) -> dict[str, int]:
        """Row counts per table (used by ``scraper stats``)."""
        with self.engine.connect() as conn:
            return {
                table.name: conn.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (STARTUPS, OPPORTUNITIES, CONTACTS, LEADS)
            }

    # ------------------------------------------------------------------ lifecycle of postings
    def close_missing_opportunities(
        self,
        *,
        source: str,
        startup_domain: str | None,
        keep_ids: Collection[int],
    ) -> int:
        """Mark ``open`` opportunities of one board as ``closed`` unless seen in this run.

        Args:
            source: Collector name (``Opportunity.source``), e.g. ``"greenhouse"``.
            startup_domain: The board's company; ``None`` closes across the whole source
                (used by HN, whose "board" is the monthly thread).
            keep_ids: Opportunity ids upserted by the latest successful run of that board.

        Returns:
            Number of opportunities closed. A closed posting that reappears is reopened by the
            next upsert (``status`` is overwritten on conflict).
        """
        conditions = [
            OPPORTUNITIES.c.source == source,
            OPPORTUNITIES.c.status == OpportunityStatus.OPEN.value,
        ]
        if keep_ids:
            conditions.append(OPPORTUNITIES.c.id.not_in(list(keep_ids)))
        with self.engine.begin() as conn:
            if startup_domain is not None:
                startup_id = conn.execute(
                    select(STARTUPS.c.id).where(
                        STARTUPS.c.canonical_domain == canonical_domain(startup_domain)
                    )
                ).scalar_one_or_none()
                if startup_id is None:
                    return 0
                conditions.append(OPPORTUNITIES.c.startup_id == startup_id)
            stmt = (
                update(OPPORTUNITIES)
                .where(and_(*conditions))
                .values(status=OpportunityStatus.CLOSED.value)
            )
            return conn.execute(stmt).rowcount or 0

    # ------------------------------------------------------------------ filtered queries
    def query_opportunities(self, flt: ExportFilter) -> list[tuple[Opportunity, Startup | None]]:
        """Opportunities matching ``flt`` with their startup, best hidden score first."""
        o, s = OPPORTUNITIES, STARTUPS
        conditions: list[ColumnElement[bool]] = []
        if flt.status is not None:
            conditions.append(o.c.status == flt.status.value)
        place: ColumnElement[bool] = and_(
            o.c.city_canonical.in_(sorted(flt.cities)) if flt.cities else true(),
            o.c.work_mode.in_(sorted(m.value for m in flt.work_modes))
            if flt.work_modes
            else true(),
        )
        if flt.include_remote:
            place = or_(place, o.c.work_mode == WorkMode.REMOTE_INDIA.value)
        conditions.append(place)
        if flt.min_hidden_score is not None:
            conditions.append(s.c.hidden_score >= flt.min_hidden_score)
        stmt = (
            select(o)
            .select_from(o.outerjoin(s, o.c.startup_id == s.c.id))
            .where(and_(*conditions))
            .order_by(
                s.c.hidden_score.desc().nulls_last(), o.c.posted_at.desc().nulls_last(), o.c.id
            )
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        opportunities = [_to_model(Opportunity, row) for row in rows]
        startups = self._startups_by_id({op.startup_id for op in opportunities if op.startup_id})
        return [
            (op, startups.get(op.startup_id) if op.startup_id else None) for op in opportunities
        ]

    def query_startups(self, flt: ExportFilter) -> list[Startup]:
        """Startups matching ``flt`` (HQ city / Remote-India roles / score), best score first."""
        stmt = (
            select(STARTUPS)
            .where(self._startup_conditions(flt))
            .order_by(STARTUPS.c.hidden_score.desc().nulls_last(), STARTUPS.c.name)
        )
        with self.engine.connect() as conn:
            return [_to_model(Startup, r) for r in conn.execute(stmt).mappings().all()]

    def query_leads(self, flt: ExportFilter) -> list[StartupLead]:
        """Leads in ``flt.cities`` (HQ); ``min_hidden_score`` applies via the linked startup."""
        conditions: list[ColumnElement[bool]] = []
        if flt.cities:
            conditions.append(LEADS.c.hq_city_canonical.in_(sorted(flt.cities)))
        stmt = select(LEADS).select_from(
            LEADS.outerjoin(STARTUPS, LEADS.c.startup_id == STARTUPS.c.id)
        )
        if flt.min_hidden_score is not None:
            conditions.append(STARTUPS.c.hidden_score >= flt.min_hidden_score)
        stmt = stmt.where(and_(true(), *conditions)).order_by(
            LEADS.c.last_funded_at.desc().nulls_last(), LEADS.c.name
        )
        with self.engine.connect() as conn:
            return [_to_model(StartupLead, r) for r in conn.execute(stmt).mappings().all()]

    def query_contacts(self, flt: ExportFilter) -> list[tuple[Contact, Startup]]:
        """Contacts of startups matching ``flt``, most promising startup and confidence first."""
        stmt = (
            select(CONTACTS)
            .select_from(CONTACTS.join(STARTUPS, CONTACTS.c.startup_id == STARTUPS.c.id))
            .where(self._startup_conditions(flt))
            .order_by(
                STARTUPS.c.hidden_score.desc().nulls_last(),
                STARTUPS.c.name,
                CONTACTS.c.confidence_score.desc(),
            )
        )
        with self.engine.connect() as conn:
            contacts = [_to_model(Contact, r) for r in conn.execute(stmt).mappings().all()]
        startups = self._startups_by_id({c.startup_id for c in contacts})
        return [(c, startups[c.startup_id]) for c in contacts]

    def open_opportunity_counts(self) -> dict[int, int]:
        """``startup_id -> number of open opportunities``."""
        stmt = (
            select(OPPORTUNITIES.c.startup_id, func.count())
            .where(
                OPPORTUNITIES.c.status == OpportunityStatus.OPEN.value,
                OPPORTUNITIES.c.startup_id.is_not(None),
            )
            .group_by(OPPORTUNITIES.c.startup_id)
        )
        with self.engine.connect() as conn:
            return {sid: n for sid, n in conn.execute(stmt).all()}

    def stats(self) -> dict[str, Any]:
        """Raw breakdowns for ``scraper stats`` (labels/thresholds are applied by the caller)."""
        o = OPPORTUNITIES
        is_open = o.c.status == OpportunityStatus.OPEN.value

        def grouped(column: Any, *where: Any) -> dict[str, int]:
            stmt = (
                select(column, func.count())
                .where(*where)
                .group_by(column)
                .order_by(func.count().desc())
            )
            with self.engine.connect() as conn:
                return {
                    str(k) if k is not None else "unknown": n for k, n in conn.execute(stmt).all()
                }

        with self.engine.connect() as conn:
            scores = [row[0] for row in conn.execute(select(STARTUPS.c.hidden_score)).all()]
            leads_with_domain = conn.execute(
                select(func.count()).select_from(LEADS).where(LEADS.c.domain.is_not(None))
            ).scalar_one()
        return {
            "counts": self.counts(),
            "opportunities_by_status": grouped(o.c.status),
            "open_by_city": grouped(o.c.city_canonical, is_open),
            "open_by_work_mode": grouped(o.c.work_mode, is_open),
            "open_by_source": grouped(o.c.source, is_open),
            "startup_scores": scores,
            "leads_with_domain": leads_with_domain,
            "contacts_by_method": grouped(CONTACTS.c.source_method),
        }

    def _startup_conditions(self, flt: ExportFilter) -> ColumnElement[bool]:
        s, o = STARTUPS, OPPORTUNITIES
        open_role = and_(o.c.startup_id == s.c.id, o.c.status == OpportunityStatus.OPEN.value)
        conditions: list[ColumnElement[bool]] = []
        place: ColumnElement[bool] = true()
        if flt.cities:
            place = s.c.hq_city_canonical.in_(sorted(flt.cities))
        if flt.work_modes:
            modes = sorted(m.value for m in flt.work_modes)
            place = and_(place, exists().where(open_role, o.c.work_mode.in_(modes)))
        if flt.include_remote:
            remote = exists().where(open_role, o.c.work_mode == WorkMode.REMOTE_INDIA.value)
            place = or_(place, remote)
        conditions.append(place)
        if flt.min_hidden_score is not None:
            conditions.append(s.c.hidden_score >= flt.min_hidden_score)
        return and_(*conditions)

    def _startups_by_id(self, ids: Collection[int]) -> dict[int, Startup]:
        if not ids:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(STARTUPS).where(STARTUPS.c.id.in_(sorted(ids)))).mappings()
            return {row["id"]: _to_model(Startup, row) for row in rows.all()}

    # ------------------------------------------------------------------ helpers
    def _get_one[M: BaseModel](self, model_cls: type[M], table: Table, where: Any) -> M | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(table).where(where)).mappings().first()
        return _to_model(model_cls, row) if row else None

    def _list[M: BaseModel](
        self, model_cls: type[M], table: Table, where: Any | None = None
    ) -> list[M]:
        stmt = select(table).order_by(table.c.id)
        if where is not None:
            stmt = stmt.where(where)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_to_model(model_cls, row) for row in rows]
