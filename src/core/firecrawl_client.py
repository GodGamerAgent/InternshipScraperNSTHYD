"""Optional Tier 3 fallback: render JavaScript-only pages through Firecrawl.

Active **only** when ``FIRECRAWL_API_KEY`` is set (``FirecrawlClient.from_settings`` returns
``None`` otherwise, and every caller must handle that). Used strictly for pages that plain
``httpx`` returns as an empty SPA shell (``looks_js_only``), e.g. Webflow/React directories.

Compliance: robots.txt of the *target* site is checked with our own User-Agent before the URL is
sent to Firecrawl — delegating the fetch never widens what we are allowed to crawl. Requests to
the Firecrawl API go through the shared ``HttpClient`` (pacing, retries). The API key is only sent
in the ``Authorization`` header and never logged or included in ``repr``.
"""

from typing import Any, Protocol, Self

from src.core.config import Settings
from src.core.http import FetchError, HttpClient, RobotsDisallowedError

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v1"


class HtmlRenderer(Protocol):
    """Anything that can return rendered HTML for a URL (Firecrawl, or a test double)."""

    async def scrape_html(self, url: str) -> str: ...


class FirecrawlClient:
    """Minimal Firecrawl ``/scrape`` client returning rendered HTML."""

    def __init__(self, http: HttpClient, api_key: str, *, base_url: str = DEFAULT_BASE_URL) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("FirecrawlClient needs a non-empty API key")
        self.http = http
        self._api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"FirecrawlClient(base_url={self.base_url!r}, api_key=***)"

    @classmethod
    def from_settings(cls, http: HttpClient, settings: Settings | None = None) -> Self | None:
        """A client if ``FIRECRAWL_API_KEY`` is set, else ``None`` (feature disabled)."""
        settings = settings or Settings.from_env()
        if not settings.firecrawl_api_key:
            return None
        return cls(http, settings.firecrawl_api_key)

    async def scrape_html(self, url: str) -> str:
        """Rendered HTML of ``url``.

        Raises:
            RobotsDisallowedError: The target's robots.txt forbids our crawler.
            FetchError: Firecrawl failed or returned no HTML.
        """
        if not await self.http.robots.can_fetch(url):
            raise RobotsDisallowedError(url)
        reply: Any = await self.http.post_json(
            f"{self.base_url}/scrape",
            {"url": url, "formats": ["html"], "onlyMainContent": False},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if not isinstance(reply, dict) or reply.get("success") is False:
            error = reply.get("error") if isinstance(reply, dict) else "malformed reply"
            raise FetchError(url, None, f"firecrawl scrape failed: {error}")
        html = (reply.get("data") or {}).get("html") or ""
        if not html.strip():
            raise FetchError(url, None, "firecrawl returned no HTML")
        return html

    async def search(self, query: str, limit: int = 10) -> Any:
        """Raw Firecrawl ``/search`` reply for ``query`` (parsed by ``src.core.search``)."""
        return await self.http.post_json(
            f"{self.base_url}/search",
            {"query": query, "limit": max(1, min(limit, 50))},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
