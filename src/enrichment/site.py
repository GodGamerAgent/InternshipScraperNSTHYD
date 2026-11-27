"""Fetch a startup's own pages once, so ATS discovery and contact extraction share them.

Every page is an HTML page on the startup's site, so robots.txt is always respected (a disallowed
path is recorded, never worked around). A page that comes back as an empty SPA shell
(``looks_js_only``) is re-fetched through the optional renderer (Firecrawl) when one is given.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from src.core.firecrawl_client import HtmlRenderer
from src.core.http import FetchError, HttpClient, RobotsDisallowedError
from src.core.text import looks_js_only

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SitePage:
    """One probed path. ``html`` is empty unless the page was fetched with status 200."""

    path: str
    url: str
    final_url: str  # after redirects (careers pages often redirect to an ATS board)
    status: int | None
    html: str = ""
    error: str | None = None  # "robots_disallowed", "FetchError: …"
    js_only: bool = False
    rendered: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)


async def fetch_site_pages(
    http: HttpClient,
    domain: str,
    paths: Sequence[str],
    *,
    max_pages: int = 9,
    spa_min_text_chars: int = 400,
    renderer: HtmlRenderer | None = None,
) -> list[SitePage]:
    """GET ``https://<domain><path>`` for each path (sequential: same host is serialized anyway)."""
    pages: list[SitePage] = []
    for path in list(dict.fromkeys(paths))[:max_pages]:
        url = f"https://{domain}{path}"
        try:
            response = await http.get(url)  # robots.txt respected
        except RobotsDisallowedError:
            pages.append(SitePage(path, url, url, None, error="robots_disallowed"))
            continue
        except FetchError as exc:
            pages.append(SitePage(path, url, url, exc.status, error=f"FetchError: {exc}"[:200]))
            continue
        final_url = str(response.url)
        if response.status_code != 200:
            pages.append(SitePage(path, url, final_url, response.status_code))
            continue
        html = response.text
        js_only = looks_js_only(html, spa_min_text_chars)
        rendered = False
        if js_only and renderer is not None:
            try:
                html = await renderer.scrape_html(url)
                rendered = True
            except FetchError as exc:
                logger.info("site.render_failed", extra={"url": url, "error": str(exc)[:200]})
        pages.append(SitePage(path, url, final_url, 200, html, None, js_only, rendered))
    logger.debug(
        "site.fetched",
        extra={"domain": domain, "pages": len(pages), "ok": sum(p.ok for p in pages)},
    )
    return pages
