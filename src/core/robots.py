"""RFC 9309 (Robots Exclusion Protocol) parser and cached per-origin checker.

Why not ``urllib.robotparser``: it applies the *first* matching rule and has no ``*`` / ``$``
wildcards, while RFC 9309 requires the *longest* match (ties go to ``allow``) and wildcards.

Behaviour summary (RFC section in brackets):

* Groups are one or more ``user-agent`` lines followed by rules; groups naming our product token
  are merged; otherwise the ``*`` groups are merged; no group -> everything allowed. [2.2.1]
* Longest matching pattern (in octets) wins; on a tie ``allow`` wins. ``*`` matches any sequence,
  a trailing ``$`` anchors the end. Empty ``disallow:`` is no rule. [2.2.2]
* ``/robots.txt`` itself is always allowed. [2.2.2]
* 2xx -> parse; 4xx -> no restrictions ("unavailable"); 5xx / network error -> disallow all
  ("unreachable"). We also treat 429 as unreachable to stay conservative. [2.3.1]
* At most 500 KiB is parsed. [2.5]  Results are cached for at most 24 h. [2.4]
* ``crawl-delay`` (non-standard but common) is exposed so the HTTP client can honour it.
"""

import asyncio
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

logger = logging.getLogger(__name__)

MAX_ROBOTS_BYTES = 500 * 1024
DEFAULT_TTL_S = 24 * 60 * 60
UNREACHABLE_TTL_S = 15 * 60
_TOKEN = re.compile(r"[A-Za-z_-]+")
# Common misspellings RFC 9309 §2.2 says crawlers MAY accept.
_KEY_ALIASES = {
    "useragent": "user-agent",
    "user agent": "user-agent",
    "dissallow": "disallow",
    "disalow": "disallow",
    "crawldelay": "crawl-delay",
}
# Characters left as-is when normalizing paths/patterns: RFC 3986 reserved + unreserved, plus the
# robots wildcards. Everything else (spaces, non-ASCII) is UTF-8 percent-encoded.
_SAFE = "/?#[]@!$&'()*+,;=:-._~"


def product_token(user_agent: str) -> str:
    """Product token of a User-Agent string, lowercased.

    ``"india-internship-scraper/0.1 (+https://…)"`` -> ``"india-internship-scraper"``.

    Raises:
        ValueError: If the string does not start with a valid token (``[A-Za-z_-]+``).
    """
    match = _TOKEN.match(user_agent.strip())
    if not match:
        raise ValueError(f"User-Agent must start with a product token, got {user_agent!r}")
    return match.group().lower()


def _normalize(path: str) -> str:
    return quote(unquote(path), safe=_SAFE)


def _path_of(url_or_path: str) -> str:
    parts = urlsplit(url_or_path)
    path = parts.path or "/"
    return _normalize(f"{path}?{parts.query}" if parts.query else path)


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @classmethod
    def build(cls, allow: bool, raw_pattern: str) -> "_Rule":
        pattern = raw_pattern if raw_pattern.startswith(("/", "*")) else f"/{raw_pattern}"
        pattern = _normalize(pattern)
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return cls(allow, pattern, re.compile(regex + (r"\Z" if anchored else ""), re.DOTALL))

    @property
    def specificity(self) -> int:
        return len(self.pattern.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class _Group:
    agents: tuple[str, ...]
    rules: tuple[_Rule, ...]
    crawl_delay: float | None


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """The rules that apply to *one* crawler on *one* origin."""

    rules: tuple[_Rule, ...] = ()
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = ()
    disallow_all: bool = False

    @classmethod
    def allow_everything(cls) -> "RobotsRules":
        """No restrictions (missing robots.txt / 4xx)."""
        return cls()

    @classmethod
    def block_everything(cls) -> "RobotsRules":
        """Complete disallow (server unreachable / 5xx)."""
        return cls(disallow_all=True)

    @classmethod
    def parse(cls, text: str, user_agent: str) -> "RobotsRules":
        """Parse robots.txt ``text`` and keep only the groups that apply to ``user_agent``."""
        token = product_token(user_agent)
        groups, sitemaps = _parse_groups(text.lstrip("﻿"))
        selected = [g for g in groups if token in g.agents] or [
            g for g in groups if "*" in g.agents
        ]
        delays = [g.crawl_delay for g in selected if g.crawl_delay is not None]
        return cls(
            rules=tuple(rule for g in selected for rule in g.rules),
            crawl_delay=max(delays) if delays else None,
            sitemaps=tuple(sitemaps),
        )

    def is_allowed(self, url_or_path: str) -> bool:
        """Whether a URL (or path + query) may be fetched under these rules."""
        if self.disallow_all:
            return False
        path = _path_of(url_or_path)
        if path == "/robots.txt":
            return True
        best: _Rule | None = None
        for rule in self.rules:
            if not rule.regex.match(path):
                continue
            if (
                best is None
                or rule.specificity > best.specificity
                or (rule.specificity == best.specificity and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow


def _parse_groups(text: str) -> tuple[list[_Group], list[str]]:
    groups: list[_Group] = []
    sitemaps: list[str] = []
    agents: list[str] = []
    rules: list[_Rule] = []
    delay: float | None = None
    in_rules = False

    def close() -> None:
        if agents:
            groups.append(_Group(tuple(agents), tuple(rules), delay))

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = _KEY_ALIASES.get(key.lower(), key.lower())
        if key == "user-agent":
            if in_rules:  # a user-agent after rules starts a new group
                close()
                agents, rules, delay, in_rules = [], [], None, False
            match = _TOKEN.match(value)
            token = match.group().lower() if match else ""
            agents.append("*" if value.startswith("*") else token)
        elif key in {"allow", "disallow"} and agents:
            in_rules = True
            if value:  # empty value means "no rule"
                rules.append(_Rule.build(key == "allow", value))
        elif key == "crawl-delay" and agents:
            in_rules = True
            try:
                delay = max(0.0, float(value))
            except ValueError:
                logger.debug("robots.bad_crawl_delay", extra={"value": value})
        elif key == "sitemap" and value:
            sitemaps.append(value)
    close()
    return groups, sitemaps


# ---------------------------------------------------------------------------- checker
@dataclass(frozen=True, slots=True)
class RobotsFetch:
    """Outcome of fetching ``/robots.txt``. ``status=None`` means a network error."""

    status: int | None
    content: bytes = b""


RobotsFetcher = Callable[[str], Awaitable[RobotsFetch]]


@dataclass(slots=True)
class _CacheEntry:
    rules: RobotsRules
    expires_at: float


@dataclass
class RobotsChecker:
    """Fetches, interprets and caches robots.txt per origin (``scheme://host[:port]``).

    Args:
        fetcher: Coroutine that GETs a robots.txt URL (the HTTP client supplies a rate-limited one).
        user_agent: Our User-Agent; its product token selects the group.
        ttl_s: Cache lifetime for successfully fetched/4xx results (RFC 9309: ≤ 24 h).
        unreachable_ttl_s: Shorter lifetime for "disallow all" results so we retry sooner.
        clock: Monotonic clock (injectable for tests).
    """

    fetcher: RobotsFetcher
    user_agent: str
    ttl_s: float = DEFAULT_TTL_S
    unreachable_ttl_s: float = UNREACHABLE_TTL_S
    clock: Callable[[], float] = time.monotonic
    _cache: dict[str, _CacheEntry] = field(default_factory=dict, init=False, repr=False)
    _locks: defaultdict[str, asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock), init=False, repr=False
    )

    def __post_init__(self) -> None:
        product_token(self.user_agent)  # fail fast on an unusable UA
        if self.ttl_s > DEFAULT_TTL_S:
            raise ValueError("RFC 9309 §2.4: robots.txt should not be cached for more than 24 h")

    @staticmethod
    def origin_of(url: str) -> str:
        """``https://www.acme.in:8443/x`` -> ``https://www.acme.in:8443``."""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"not an absolute http(s) URL: {url!r}")
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname}{port}"

    async def rules_for(self, url: str) -> RobotsRules:
        """Rules for the origin of ``url`` (fetched once per TTL, concurrency-safe)."""
        origin = self.origin_of(url)
        if (entry := self._fresh(origin)) is not None:
            return entry.rules
        async with self._locks[origin]:
            if (entry := self._fresh(origin)) is not None:  # another task fetched it meanwhile
                return entry.rules
            fetched = await self.fetcher(f"{origin}/robots.txt")
            rules, ttl = self._interpret(fetched)
            self._cache[origin] = _CacheEntry(rules, self.clock() + ttl)
            logger.debug(
                "robots.loaded",
                extra={
                    "origin": origin,
                    "status": fetched.status,
                    "rules": len(rules.rules),
                    "disallow_all": rules.disallow_all,
                },
            )
            return rules

    async def can_fetch(self, url: str) -> bool:
        """Whether our crawler may fetch ``url``."""
        return (await self.rules_for(url)).is_allowed(url)

    def _fresh(self, origin: str) -> _CacheEntry | None:
        entry = self._cache.get(origin)
        return entry if entry is not None and entry.expires_at > self.clock() else None

    def _interpret(self, fetched: RobotsFetch) -> tuple[RobotsRules, float]:
        status = fetched.status
        if status is None or status == 429 or status >= 500:
            return RobotsRules.block_everything(), self.unreachable_ttl_s
        if 200 <= status < 300:
            text = fetched.content[:MAX_ROBOTS_BYTES].decode("utf-8", errors="replace")
            return RobotsRules.parse(text, self.user_agent), self.ttl_s
        # 4xx "unavailable" (and leftover 3xx after the redirect limit): no restrictions.
        return RobotsRules.allow_everything(), self.ttl_s
