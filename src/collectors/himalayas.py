"""Himalayas remote internships open to India (ADR 010).

Source: the documented, keyless public search API (himalayas.app/api).
``GET https://himalayas.app/jobs/api/search?country=IN&employment_type=Intern&sort=recent``, plus
any other ``sources.yaml: remote_feeds.himalayas.queries``. Shape verified 2026-11-26:

* ``{"jobs": [...], "totalCount", "offset", "limit"}``, 20 jobs per page.
* Each job has ``title``, ``companyName``, ``employmentType`` ("Intern"), ``minSalary`` /
  ``maxSalary``, ``currency``, ``salaryPeriod`` ("monthly", "annual", …), ``locationRestrictions``
  (country names; ``[]`` = worldwide), ``timezoneRestrictions`` (UTC offsets; 5.5 = IST),
  ``description`` (HTML), ``pubDate`` (epoch **seconds**), ``applicationLink`` and ``guid``.

Only **page 1** of each query is read: later pages need ``?page=N``, which himalayas.app's
robots.txt disallows. robots.txt is respected for every request. Per the API terms,
``applicationLink`` is stored unchanged as the opportunity URL, which links back to Himalayas.

All queries form one target, so postings that disappeared are closed (``close_scope = "*"``) only
after a run in which every query succeeded.
"""

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from src.collectors.config import HimalayasConfig, RemoteFeedsConfig
from src.collectors.remote_base import IndiaOpenness, remote_opportunity
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.core.text import html_to_text
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.enrichment.stipend import stipend_from_structured
from src.models import CollectedOpportunity

logger = logging.getLogger(__name__)


def _offsets(value: Any) -> list[float]:
    return [float(v) for v in value or [] if isinstance(v, int | float) and not isinstance(v, bool)]


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class HimalayasCollector(BaseCollector[HimalayasConfig, list[Any], CollectedOpportunity]):
    """Single target: every configured search query (page 1 each)."""

    name = "himalayas"

    def __init__(
        self,
        http: HttpClient,
        *,
        config: RemoteFeedsConfig,
        geo: GeoResolver,
        matcher: InternshipMatcher,
    ) -> None:
        super().__init__(http)
        self.config = config
        self.openness = IndiaOpenness(config, geo)
        self.matcher = matcher

    def targets(self) -> list[HimalayasConfig]:
        return [self.config.himalayas] if self.config.himalayas is not None else []

    def describe_target(self, target: HimalayasConfig) -> str:
        return "search"

    def close_scope(self, target: HimalayasConfig) -> str | None:
        return "*"

    async def fetch(self, target: HimalayasConfig) -> list[Any]:
        # Documented public API; robots.txt respected (default), page 1 only (robots: no ?page=).
        return [await self.http.get_json(target.search_url, params=q) for q in target.queries]

    def parse(self, raw: list[Any], target: HimalayasConfig) -> Iterable[CollectedOpportunity]:
        items: dict[str, CollectedOpportunity] = {}
        for page in raw:
            jobs = page.get("jobs") if isinstance(page, Mapping) else None
            if not isinstance(jobs, list):
                raise ValueError("expected {'jobs': [...]} from the Himalayas search API")
            for job in jobs:
                if not isinstance(job, Mapping):
                    continue
                try:
                    item = self._item(job)
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    logger.warning(
                        "collector.posting_skipped",
                        extra={"collector": self.name, "target": "search",
                               "error_type": type(exc).__name__, "error": str(exc)[:300]},
                    )  # fmt: skip
                    continue
                if item is not None:
                    items.setdefault(item.opportunity.url, item)
        return list(items.values())

    def _item(self, job: Mapping[str, Any]) -> CollectedOpportunity | None:
        title = " ".join(str(job["title"]).split())
        if not self.matcher.is_internship(title, employment_type=job.get("employmentType")):
            return None
        location = self.openness.structured(
            [str(c) for c in job.get("locationRestrictions") or []],
            _offsets(job.get("timezoneRestrictions")),
        )
        if location is None:
            return None
        published = job.get("pubDate")
        company = str(job.get("companyName") or "").strip()
        body = html_to_text(job.get("description"))
        return remote_opportunity(
            source=self.name,
            title=title,
            url=str(job["applicationLink"] or job["guid"]),
            description=f"{company}\n{body}".strip() if company else body,
            location=location,
            posted_at=datetime.fromtimestamp(published, UTC)
            if isinstance(published, int | float) and not isinstance(published, bool)
            else None,
            stipend=stipend_from_structured(
                _number(job.get("minSalary")),
                _number(job.get("maxSalary")),
                job.get("currency"),
                job.get("salaryPeriod"),
            ),
        )
