"""Query filters shared by the repository's ``query_*`` methods and the ``export`` CLI."""

from dataclasses import dataclass

from src.models import OpportunityStatus, WorkMode


@dataclass(frozen=True, slots=True)
class ExportFilter:
    """Which records an export or listing should include.

    Semantics (``docs/ARCHITECTURE.md`` §10):

    * ``cities`` — canonical slugs; ``None`` means every city.
    * ``work_modes`` — applied together with ``cities`` (``AND``).
    * ``include_remote`` — additionally include Remote-India roles regardless of ``cities`` /
      ``work_modes`` (``(city AND mode) OR remote_india``), as in ``--cities hyderabad
      --include-remote``. For startups/contacts it means "has an open Remote-India role".
    * ``min_hidden_score`` — startup score ≥ N; records without a scored startup are excluded.
    * ``status`` — opportunity status; ``None`` means open and closed.
    """

    cities: frozenset[str] | None = None
    work_modes: frozenset[WorkMode] | None = None
    include_remote: bool = False
    min_hidden_score: int | None = None
    status: OpportunityStatus | None = OpportunityStatus.OPEN
