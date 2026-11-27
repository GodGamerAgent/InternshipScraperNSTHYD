"""Hacker News "Ask HN: Who is hiring?" collector (India / IST / worldwide-remote, junior roles).

Uses the documented HN Search API (https://hn.algolia.com/api): find the latest thread by the
``whoishiring`` account, then fetch its comments; only top-level comments are postings.
hn.algolia.com has no robots.txt (404 = allow), and robots is still checked by default.

Filters (``sources.yaml: hacker_news``), applied to each posting's header line
(``Company | Role | Location | …``):

* **job-seeker posts are skipped first** (``seeker_patterns``) — they describe individuals;
* the header must contain a ``role_keywords`` match (intern, junior, new grad, fresher, …);
* the location must resolve to India / Remote-India, or be remote and say worldwide/global.

The company domain is the first link in the post that is not a job board / social site.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from src.collectors.ats_base import parse_timestamp
from src.collectors.config import HackerNewsConfig
from src.core.base import BaseCollector
from src.core.http import HttpClient
from src.core.text import html_to_text
from src.core.urls import canonical_domain, host_in
from src.enrichment.geo import GeoResolver
from src.enrichment.stipend import extract_internship_terms
from src.models import CollectedOpportunity, Opportunity, Startup, WorkMode

HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"
MAX_DESCRIPTION_CHARS = 4000


class HnHiringCollector(BaseCollector[None, dict, CollectedOpportunity]):
    name = "hn_hiring"

    def __init__(
        self,
        http: HttpClient,
        *,
        config: HackerNewsConfig,
        geo: GeoResolver,
        non_company_hosts: list[str] | None = None,
    ) -> None:
        super().__init__(http)
        self.config = config
        self.non_company_hosts = list(non_company_hosts or [])
        self.geo = geo
        self._title = re.compile(config.story_title_pattern, re.IGNORECASE)
        self._roles = [re.compile(p, re.IGNORECASE) for p in config.role_keywords]
        self._seeker = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in config.seeker_patterns]
        self._global = [k.lower() for k in config.remote_global_keywords]

    def describe_target(self, target: None) -> str:
        return "latest_thread"

    def close_scope(self, target: None) -> str | None:
        return "*"  # postings from older monthly threads are closed once a new thread is read

    async def fetch(self, target: None) -> dict:
        stories = await self.http.get_json(
            self.config.search_url,
            params={"tags": f"story,author_{self.config.who_is_hiring_author}", "hitsPerPage": 10},
        )
        story = next(
            (h for h in stories.get("hits", []) if self._title.search(h.get("title") or "")), None
        )
        if story is None:
            raise ValueError("no 'Who is hiring?' thread found")
        comments = await self.http.get_json(
            self.config.comments_url,
            params={
                "tags": f"comment,story_{story['objectID']}",
                "hitsPerPage": self.config.max_comments,
            },
        )
        return {"story": story, "comments": comments.get("hits", [])}

    def parse(self, raw: dict, target: None) -> Iterable[CollectedOpportunity]:
        story_id = int(raw["story"]["objectID"])
        items: list[CollectedOpportunity] = []
        for comment in raw["comments"]:
            if comment.get("parent_id") != story_id or not comment.get("comment_text"):
                continue
            item = self._posting(comment)
            if item is not None:
                items.append(item)
        return items

    # -------------------------------------------------------------- helpers
    def is_seeker_post(self, text: str) -> bool:
        return any(p.search(text) for p in self._seeker)

    def _posting(self, comment: Mapping[str, Any]) -> CollectedOpportunity | None:
        html = comment["comment_text"]
        text = html_to_text(html)
        if not text or self.is_seeker_post(text):
            return None
        header = text.split("\n", 1)[0][:300]
        segments = [s.strip() for s in header.split("|") if s.strip()]
        role = next((s for s in segments[1:] if any(p.search(s) for p in self._roles)), None)
        if role is None and any(p.search(header) for p in self._roles):
            role = segments[1] if len(segments) > 1 else header
        if role is None:
            return None

        location = self.geo.resolve(" | ".join(segments[1:]) or header)
        worldwide = location.work_mode is WorkMode.REMOTE_GLOBAL and any(
            k in header.lower() for k in self._global
        )
        if not (location.is_india or worldwide):
            return None

        company = segments[0] if segments else "Unknown"
        startup = self._startup(company, html)
        terms = extract_internship_terms(text)
        opportunity = Opportunity(
            source=self.name,
            title=role[:300],
            url=HN_ITEM_URL.format(id=comment["objectID"]),
            description=text[:MAX_DESCRIPTION_CHARS],
            stipend_inr_min=terms.stipend.min_inr,
            stipend_inr_max=terms.stipend.max_inr,
            is_paid=terms.stipend.is_paid,
            duration_months=terms.duration_months,
            has_ppo=terms.has_ppo,
            posted_at=parse_timestamp(comment.get("created_at")),
        ).with_location(location.model_copy(update={"raw_location": header}))
        return CollectedOpportunity(startup=startup, opportunity=opportunity)

    def _startup(self, company: str, html: str) -> Startup | None:
        for anchor in BeautifulSoup(html, "lxml").find_all("a", href=True):
            href = anchor["href"]
            if not urlsplit(href).hostname or host_in(href, self.non_company_hosts):
                continue
            try:
                domain = canonical_domain(href)
            except ValueError:
                continue
            return Startup(
                name=company[:200] or domain,
                canonical_domain=domain,
                website_url=f"https://{domain}",
                discovered_via=self.name,
            )
        return None
