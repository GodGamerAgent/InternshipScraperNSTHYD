"""URL and domain normalization — the building blocks of every deduplication key."""

import re
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_WWW_PREFIX = re.compile(r"^www\d*\.")
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TRACKING_PARAMS = frozenset(
    {"gclid", "fbclid", "mc_cid", "mc_eid", "lever-source", "lever-origin", "ref_src"}
)


def _split(value: str):
    text = value.strip()
    if not text:
        raise ValueError("empty URL or domain")
    if "://" not in text:
        text = "//" + text  # let urlsplit treat bare hosts ("acme.in/careers") as netloc
    return urlsplit(text)


def canonical_domain(value: str) -> str:
    """Reduce a URL or hostname to the startup's canonical domain.

    ``"https://www.Acme.in:443/careers?x=1"`` -> ``"acme.in"``. Lowercases, strips ``www``/``www2``
    prefixes, ports, credentials and trailing dots, and IDNA-encodes unicode hosts. Other subdomains
    are kept on purpose (``app.acme.in`` stays distinct) because collapsing them correctly needs the
    Public Suffix List (``.co.in``) — revisit via ADR if that becomes a dedup problem.

    Raises:
        ValueError: If no valid hostname can be extracted.
    """
    host = (_split(value).hostname or "").rstrip(".")
    if not host:
        raise ValueError(f"no hostname in {value!r}")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"invalid hostname in {value!r}") from exc
    host = _WWW_PREFIX.sub("", host)
    labels = host.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.match(label) for label in labels):
        raise ValueError(f"invalid hostname in {value!r}")
    return host


def normalize_url(url: str) -> str:
    """Normalize a URL for fingerprinting.

    Forces ``https``, lowercases the host, strips ``www.``, default ports, fragments, trailing
    slashes and tracking parameters (``utm_*``, ``gclid``, …), and sorts the remaining query.
    """
    parts = _split(url)
    host = _WWW_PREFIX.sub("", (parts.hostname or "").rstrip("."))
    if not host:
        raise ValueError(f"no hostname in {url!r}")
    netloc = host if parts.port in (None, 80, 443) else f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
        )
    )
    return urlunsplit(("https", netloc, path, query, ""))


def is_http_url(value: str) -> bool:
    """True if ``value`` is an absolute http(s) URL with a host."""
    parts = urlsplit(value.strip())
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def host_in(url_or_host: str, hosts: Iterable[str]) -> bool:
    """True if the URL's host equals, or is a subdomain of, any of ``hosts``."""
    host = (_split(url_or_host).hostname or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in (h.lower() for h in hosts))
