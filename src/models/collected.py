"""``CollectedOpportunity`` — what collectors emit before anything is persisted."""

from src.models.base import DomainModel
from src.models.opportunity import Opportunity
from src.models.startup import Startup


class CollectedOpportunity(DomainModel):
    """An opportunity plus the startup it belongs to.

    Collectors cannot know database ids, so ``opportunity.startup_id`` is ``None`` here; the
    pipeline upserts ``startup`` first and then the opportunity with the returned id.
    ``startup`` is ``None`` for sources without a reliable company domain (e.g. some HN posts).
    """

    startup: Startup | None
    opportunity: Opportunity
