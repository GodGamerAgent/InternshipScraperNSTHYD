"""Optional web-search backends for ``scraper discover`` (ADR 007).

Only **official search APIs** are used — never a search engine's HTML results page:

* **Brave Search API** (``BRAVE_SEARCH_API_KEY``):
  ``GET https://api.search.brave.com/res/v1/web/search?q=…&count=…`` with header
  ``X-Subscription-Token``; results at ``web.results[].{url,title,description}``.
* **Firecrawl** ``/search`` (``FIRECRAWL_API_KEY``): ``POST {base}/search {"query", "limit"}``;
  results at ``data[]`` (v1) or ``data.web[]`` (v2), each with ``url`` / ``title`` /
  ``description``.

Keys travel only in request headers (never in URLs, which are logged). Both parsers are written
against the providers' documented response shapes and tested with mocks; they were not exercised
live (no keys during development).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from src.core.config import Settings
from src.core.firecrawl_client import FirecrawlClient
from src.core.http import FetchError, HttpClient

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One search result."""

    url: str
    title: str = ""
    snippet: str = ""


class SearchBackend(Protocol):
    """A search API returning result URLs for a query."""

    name: str

    async def search(self, query: str, limit: int) -> list[SearchHit]: ...


def _hits(items: Any) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in items if isinstance(items, Sequence) and not isinstance(items, str) else []:
        if isinstance(item, Mapping) and isinstance(item.get("url"), str):
            hits.append(
                SearchHit(
                    url=item["url"],
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("description") or item.get("snippet") or ""),
                )
            )
    return hits


class BraveSearch:
    """Brave Search API client (``X-Subscription-Token`` header)."""

    name = "brave"

    def __init__(self, http: HttpClient, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("BraveSearch needs a non-empty API key")
        self.http = http
        self._key = api_key.strip()

    def __repr__(self) -> str:
        return "BraveSearch(api_key=***)"

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        reply = await self.http.get_json(
            BRAVE_ENDPOINT,
            params={"q": query, "count": max(1, min(limit, 20))},
            headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
            respect_robots=False,  # documented search API (ADR 007)
            use_cache=False,
        )
        web = reply.get("web") if isinstance(reply, Mapping) else None
        return _hits(web.get("results") if isinstance(web, Mapping) else None)


class FirecrawlSearch:
    """Firecrawl ``/search`` via the existing :class:`FirecrawlClient` (same key and pacing)."""

    name = "firecrawl"

    def __init__(self, client: FirecrawlClient) -> None:
        self.client = client

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        reply = await self.client.search(query, limit)
        if not isinstance(reply, Mapping) or reply.get("success") is False:
            error = reply.get("error") if isinstance(reply, Mapping) else "malformed reply"
            raise FetchError(
                f"{self.client.base_url}/search", None, f"firecrawl search failed: {error}"
            )
        data = reply.get("data")
        if isinstance(data, Mapping):  # v2: {"data": {"web": [...]}}
            data = data.get("web")
        return _hits(data)


def search_backend_from_settings(http: HttpClient, settings: Settings) -> SearchBackend | None:
    """Brave if its key is set, else Firecrawl if its key is set, else ``None``."""
    if settings.brave_search_api_key:
        return BraveSearch(http, settings.brave_search_api_key)
    firecrawl = FirecrawlClient.from_settings(http, settings)
    return FirecrawlSearch(firecrawl) if firecrawl is not None else None


def search_backend_name(settings: Settings) -> str | None:
    """Which backend :func:`search_backend_from_settings` would pick, without any network use."""
    if settings.brave_search_api_key:
        return BraveSearch.name
    if settings.firecrawl_api_key:
        return FirecrawlSearch.name
    return None
