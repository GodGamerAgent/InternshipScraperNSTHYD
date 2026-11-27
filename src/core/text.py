"""Text helpers: normalization (geo matching, fingerprints) and HTML-to-text for descriptions."""

import re
import unicodedata
import warnings

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_SPACES = re.compile(r"[ \t\u00a0\u200b]+")


def normalize_text(value: str | None) -> str:
    """Return a casefolded, punctuation-free, single-spaced form of ``value``.

    ``"Hi-Tech City, HYD (Remote/IST)"`` -> ``"hi tech city hyd remote ist"``. Digits are kept, so
    PIN codes survive (``"500 081"``). The same function is applied to config terms and to input
    strings, which is what makes alias matching punctuation-insensitive.
    """
    if not value:
        return ""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _NON_WORD.sub(" ", folded).strip()


def html_to_text(markup: str | None) -> str:
    """Visible text of an HTML fragment, one block per line, whitespace collapsed.

    Pass already-unescaped HTML (Greenhouse double-escapes ``content``; ``html.unescape`` it first).
    """
    if not markup:
        return ""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(markup, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = (_SPACES.sub(" ", line).strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


_SPA_MOUNT = re.compile(
    r"<div[^>]+id=[\"'](?:root|app|__next|__nuxt|svelte|main-app)[\"']|ng-version=|data-reactroot",
    re.IGNORECASE,
)


def looks_js_only(markup: str | None, min_text_chars: int = 400) -> bool:
    """Heuristic: the page is an SPA shell whose content needs JavaScript to render.

    True when the visible text is shorter than ``min_text_chars`` and the markup has a typical
    SPA mount point (``<div id="root">``, ``__next``, Angular, …). Used to decide whether the
    optional Firecrawl fallback is worth calling.
    """
    if not markup:
        return True
    return len(html_to_text(markup)) < min_text_chars and bool(_SPA_MOUNT.search(markup))
