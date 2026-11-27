"""Instahyre public internship listings (ADR 011).

Source (``sources.yaml: student_portals.instahyre``): the landing page
``https://www.instahyre.com/internship-jobs/``. robots.txt has no rules for ``*``, so everything
is allowed; it is still checked on every request. Verified 2026-11-26:

* The page embeds the first results as JSON in ``<script type="application/json"
  id="initial-job-search">``. It contains ``{"params": {"job_type": "2", …}, "response": {"meta":
  {"total_count", "limit", "next"}, "objects": [...]}}``, where ``job_type=2`` means internships
  (67 on probe day).
* ``meta.next`` is the site's own relative API link
  (``/api/v1/job_search?company_size=0&isLandingPage=True&job_type=2&limit=20&offset=20``).
  It is followed as given, up to ``max_pages`` responses, and never built by hand. The endpoint
  answered without login on probe day.
* Each object has ``id``, ``title``, ``candidate_title``, ``employer.company_name``,
  ``locations`` (a string such as "Bangalore" or "Work From Home"), ``keywords`` and
  ``public_url``. There is no stipend or posting date.

**Terms of Service:** Instahyre's Terms prohibit automated crawling. The project owner chose to
collect public listings anyway (ADR 011). The collector stays polite and never bypasses logins,
CAPTCHAs or firewalls.
"""

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from pydantic import ValidationError

from src.collectors.config import InstahyreConfig
from src.collectors.portal_base import india_portal_location
from src.collectors.remote_base import remote_opportunity
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.enrichment.geo import GeoResolver
from src.enrichment.internship import InternshipMatcher
from src.models import CollectedOpportunity

logger = logging.getLogger(__name__)

NEXT_PREFIX = "/api/v1/job_search"
INTERNSHIP_JOB_TYPE = "2"


def initial_search(html: str) -> Mapping[str, Any]:
    """The ``initial-job-search`` JSON embedded in the landing page. ``ValueError`` if absent."""
    node = BeautifulSoup(html, "lxml").find("script", id="initial-job-search")
    if node is None:
        raise ValueError("no initial-job-search script: Instahyre layout changed")
    try:
        data = json.loads(node.get_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed initial-job-search JSON: {exc}") from exc
    if not isinstance(data, Mapping) or not isinstance(data.get("response"), Mapping):
        raise ValueError("initial-job-search JSON has no 'response'")
    return data


def _objects(response: Any) -> list[Mapping[str, Any]]:
    objects = response.get("objects") if isinstance(response, Mapping) else None
    if not isinstance(objects, list):
        raise ValueError("expected {'objects': [...]} from Instahyre job search")
    return [o for o in objects if isinstance(o, Mapping)]


def _next_link(response: Mapping[str, Any]) -> str | None:
    meta = response.get("meta")
    link = meta.get("next") if isinstance(meta, Mapping) else None
    if not isinstance(link, str) or not link.startswith(NEXT_PREFIX):
        return None  # only follow the site's own relative job-search links
    return link


class InstahyreCollector(BaseCollector[InstahyreConfig, list[Any], CollectedOpportunity]):
    """Single target: landing page, then the embedded ``next`` links."""

    name = "instahyre"

    def __init__(
        self,
        http: HttpClient,
        *,
        config: InstahyreConfig | None,
        geo: GeoResolver,
        matcher: InternshipMatcher,
    ) -> None:
        super().__init__(http)
        self.config = config
        self.geo = geo
        self.matcher = matcher

    def targets(self) -> list[InstahyreConfig]:
        return [self.config] if self.config is not None else []

    def describe_target(self, target: InstahyreConfig) -> str:
        return "internships"

    def close_scope(self, target: InstahyreConfig) -> str | None:
        return "*"

    async def fetch(self, target: InstahyreConfig) -> list[Any]:
        landing = initial_search(await self.http.get_text(target.landing_url))  # robots respected
        internships = str(landing.get("params", {}).get("job_type")) == INTERNSHIP_JOB_TYPE
        responses: list[Any] = [landing["response"]]
        parts = urlsplit(target.landing_url)
        while len(responses) < target.max_pages and (link := _next_link(responses[-1])):
            responses.append(await self.http.get_json(f"{parts.scheme}://{parts.netloc}{link}"))
        return [internships, responses]

    def parse(self, raw: list[Any], target: InstahyreConfig) -> Iterable[CollectedOpportunity]:
        internships, responses = bool(raw[0]), raw[1]
        items: dict[str, CollectedOpportunity] = {}
        for response in responses:
            for job in _objects(response):
                try:
                    item = self._item(job, internships=internships)
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    logger.warning(
                        "collector.posting_skipped",
                        extra={"collector": self.name, "target": "internships",
                               "error_type": type(exc).__name__, "error": str(exc)[:300]},
                    )  # fmt: skip
                    continue
                if item is not None:
                    items.setdefault(item.opportunity.url, item)
        return list(items.values())

    def _item(self, job: Mapping[str, Any], *, internships: bool) -> CollectedOpportunity | None:
        title = " ".join(str(job.get("candidate_title") or job["title"]).split())
        kind = "internship" if internships else None
        if not self.matcher.is_internship(title, employment_type=kind):
            return None
        location = india_portal_location(self.geo, str(job.get("locations") or ""))
        if location is None:
            return None
        employer = job.get("employer") if isinstance(job.get("employer"), Mapping) else {}
        keywords = ", ".join(str(k) for k in job.get("keywords") or [] if isinstance(k, str))
        details = [
            str(employer.get("company_name") or ""),
            f"Skills: {keywords}" if keywords else "",
        ]
        return remote_opportunity(
            source=self.name,
            title=title,
            url=str(job["public_url"]),
            description="\n".join(d for d in details if d),
            location=location,
            posted_at=None,
        )
