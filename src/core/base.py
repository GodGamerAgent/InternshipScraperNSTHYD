"""``BaseCollector`` — the contract every source plugin implements.

A collector turns *targets* (e.g. one company's ATS slug, one RSS feed) into validated models::

    targets()            -> which things to fetch            (sync, from config)
    fetch(target)        -> raw payload (JSON/HTML/…)         (async, via HttpClient only)
    parse(raw, target)   -> models                           (PURE: no I/O, fixture-testable)
    run()                -> CollectorResult                  (inherited; never raises)

Fault isolation: each target is fetched and parsed inside its own exception boundary with a
timeout, so one broken board or website never stops the rest. ``run_all`` adds the same boundary
around whole collectors. Collectors never write to SQLite — the caller persists ``result.items``.

Example::

    class AcmeBoardCollector(BaseCollector[str, dict, Opportunity]):
        name = "acme_board"

        def targets(self) -> list[str]:
            return ["acme", "examplelabs"]

        async def fetch(self, target: str) -> dict:
            return await self.http.get_json(f"https://api.acme-ats.com/{target}")

        def parse(self, raw: dict, target: str) -> Iterable[Opportunity]:
            ...
"""

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from src.core.http import FetchError, HttpClient

logger = logging.getLogger(__name__)

_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")
Stage = Literal["targets", "fetch", "parse", "run"]


@dataclass(frozen=True, slots=True)
class CollectorIssue:
    """One isolated failure. ``target`` is a human-readable label (``describe_target``)."""

    stage: Stage
    target: str
    error_type: str
    message: str
    status: int | None = None

    @classmethod
    def from_exception(cls, stage: Stage, target: str, exc: BaseException) -> "CollectorIssue":
        status = exc.status if isinstance(exc, FetchError) else None
        message = str(exc) or type(exc).__name__
        return cls(stage, target, type(exc).__name__, message, status)


@dataclass(slots=True)
class CollectorResult[ItemT]:
    """Outcome of one collector run. ``ok`` is ``True`` only if nothing failed."""

    collector: str
    started_at: datetime
    items: list[ItemT] = field(default_factory=list)
    issues: list[CollectorIssue] = field(default_factory=list)
    targets_total: int = 0
    targets_failed: int = 0
    duration_s: float = 0.0
    # Boards fetched+parsed successfully this run whose missing postings may be closed
    # (``BaseCollector.close_scope``; ``"*"`` = the whole source).
    closable_scopes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


class BaseCollector[TargetT, RawT, ItemT](ABC):
    """Abstract source plugin. Subclasses set ``name`` and implement ``fetch`` and ``parse``.

    Class attributes:
        name: Unique ``snake_case`` source id (used in ``Opportunity.source`` and ``--sources``).
        target_timeout_s: Max seconds to fetch one target (including polite waits); ``None`` = off.
    """

    name: ClassVar[str]
    target_timeout_s: ClassVar[float | None] = 300.0

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        name = cls.__dict__.get("name")
        if name is not None and not (isinstance(name, str) and _SLUG.match(name)):
            raise TypeError(f"{cls.__name__}.name must be a snake_case slug, got {name!r}")

    def __init__(self, http: HttpClient) -> None:
        if not isinstance(getattr(type(self), "name", None), str):
            raise TypeError(f"{type(self).__name__} must define a class attribute `name`")
        self.http = http
        self.log = logging.getLogger(f"src.collectors.{self.name}")

    # -------------------------------------------------------------- hooks
    def targets(self) -> Iterable[TargetT]:
        """What to fetch. Default: a single ``None`` target (one feed / one endpoint)."""
        return [None]  # type: ignore[list-item]

    def describe_target(self, target: TargetT) -> str:
        """Short label for logs and issues (override for dict/dataclass targets)."""
        return "default" if target is None else str(target)

    def close_scope(self, target: TargetT) -> str | None:
        """Board identity for closing vanished postings after a *successful* fetch+parse.

        Return the startup's canonical domain (one company's board), ``"*"`` (the whole source,
        e.g. a monthly HN thread), or ``None`` (default: this source never closes postings).
        """
        return None

    @abstractmethod
    async def fetch(self, target: TargetT) -> RawT:
        """Download the raw payload for ``target`` via ``self.http`` (never ``httpx`` directly)."""

    @abstractmethod
    def parse(self, raw: RawT, target: TargetT) -> Iterable[ItemT]:
        """Turn a raw payload into validated models. Must be pure (no network, no DB, no clock
        dependence beyond what is in ``raw``) so it can be unit-tested from fixtures."""

    # -------------------------------------------------------------- orchestration
    async def run(self) -> CollectorResult[ItemT]:
        """Fetch and parse every target concurrently with per-target fault isolation.

        Never raises (except on cancellation): every failure becomes a ``CollectorIssue``.
        """
        result: CollectorResult[ItemT] = CollectorResult(self.name, started_at=datetime.now(UTC))
        started = time.perf_counter()
        self.log.info("collector.start", extra={"collector": self.name})
        try:
            targets = list(self.targets())
        except Exception as exc:
            self.log.exception("collector.targets_failed", extra={"collector": self.name})
            result.issues.append(CollectorIssue.from_exception("targets", "*", exc))
            return self._finish(result, started)

        result.targets_total = len(targets)
        outcomes = await asyncio.gather(*(self._run_target(t) for t in targets))
        for target, (items, issue) in zip(targets, outcomes, strict=True):
            result.items.extend(items)
            if issue is not None:
                result.issues.append(issue)
                result.targets_failed += 1
            elif (scope := self.close_scope(target)) is not None:
                result.closable_scopes.append(scope)
        return self._finish(result, started)

    async def _run_target(self, target: TargetT) -> tuple[list[ItemT], CollectorIssue | None]:
        label = self.describe_target(target)
        context = {"collector": self.name, "target": label}
        try:
            async with asyncio.timeout(self.target_timeout_s):
                raw = await self.fetch(target)
        except Exception as exc:
            # Expected operational failures (network, 404, robots) -> warning without traceback.
            self.log.warning(
                "collector.fetch_failed",
                extra=context | {"error_type": type(exc).__name__, "error": str(exc)},
            )
            return [], CollectorIssue.from_exception("fetch", label, exc)
        try:
            items = list(self.parse(raw, target))
        except Exception as exc:
            # Parse failures are bugs or upstream schema changes -> full traceback.
            self.log.exception("collector.parse_failed", extra=context)
            return [], CollectorIssue.from_exception("parse", label, exc)
        self.log.debug("collector.target_done", extra=context | {"items": len(items)})
        return items, None

    def _finish(self, result: CollectorResult[ItemT], started: float) -> CollectorResult[ItemT]:
        result.duration_s = round(time.perf_counter() - started, 3)
        self.log.info(
            "collector.finished",
            extra={
                "collector": self.name,
                "items": len(result.items),
                "targets": result.targets_total,
                "targets_failed": result.targets_failed,
                "duration_s": result.duration_s,
            },
        )
        return result


async def run_all(collectors: Sequence[BaseCollector[Any, Any, Any]]) -> list[CollectorResult[Any]]:
    """Run collectors concurrently; a collector that crashes outside ``run()``'s own boundary
    still yields a ``CollectorResult`` with a ``run``-stage issue instead of propagating."""
    outcomes = await asyncio.gather(*(c.run() for c in collectors), return_exceptions=True)
    results: list[CollectorResult[Any]] = []
    for collector, outcome in zip(collectors, outcomes, strict=True):
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            logger.error(
                "collector.crashed",
                extra={"collector": collector.name, "error_type": type(outcome).__name__},
                exc_info=outcome,
            )
            crashed: CollectorResult[Any] = CollectorResult(
                collector.name, started_at=datetime.now(UTC)
            )
            crashed.issues.append(CollectorIssue.from_exception("run", "*", outcome))
            results.append(crashed)
        else:
            results.append(outcome)
    return results
