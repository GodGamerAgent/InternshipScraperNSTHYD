"""Polite async HTTP client — the only way collectors talk to the network.

Guarantees (``implementation.md`` §5, ``AGENTS.md`` §4):

* **One request at a time per host**, with a jittered 1.5–2.5 s gap after each one (raised to
  the host's robots.txt ``crawl-delay`` when that is larger, capped at 60 s).
* **At most 5 requests in flight** across all hosts.
* **Up to 3 retries** on 429/5xx and transport errors, with exponential backoff and jitter;
  ``Retry-After`` (seconds or HTTP date) is honoured, capped at 120 s.
* **robots.txt (RFC 9309) is checked by default**; documented public JSON APIs may opt out per
  request with ``respect_robots=False`` and a comment explaining why.
* **Conditional requests**: cached ``ETag`` / ``Last-Modified`` are sent as ``If-None-Match`` /
  ``If-Modified-Since``; a ``304`` is served from the cache (``is_from_cache(response)``).
* A **transparent User-Agent** from ``SCRAPER_USER_AGENT`` on every request.

Time (``clock``/``sleep``), randomness (``rng``) and the transport are injectable so tests run
offline and instantly.
"""

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

import httpx

from src.core.config import Settings
from src.core.robots import RobotsChecker, RobotsFetch, product_token

logger = logging.getLogger(__name__)

FROM_CACHE = "from_cache"
_CACHED_HEADERS = ("content-type", "etag", "last-modified")


# ---------------------------------------------------------------------------- errors
class FetchError(RuntimeError):
    """A request failed after all retries, or returned an unusable response.

    Attributes:
        url: The requested URL.
        status: Final HTTP status, or ``None`` for transport errors.
    """

    def __init__(self, url: str, status: int | None, message: str) -> None:
        super().__init__(f"{message} ({url}, status={status})")
        self.url = url
        self.status = status


class RobotsDisallowedError(FetchError):
    """robots.txt forbids our crawler from fetching this URL. Never retry or work around it."""

    def __init__(self, url: str) -> None:
        super().__init__(url, None, "disallowed by robots.txt")


# ---------------------------------------------------------------------------- policy
@dataclass(frozen=True, slots=True)
class HttpPolicy:
    """Politeness and reliability knobs. Defaults are the project's compliance requirements.

    ``max_concurrency`` and ``max_retries`` are capped at the spec values (5 and 3) and cannot be
    raised; delays may be changed (tests inject a fake clock instead of lowering them).
    """

    min_delay_s: float = 1.5
    max_delay_s: float = 2.5
    max_concurrency: int = 5
    max_retries: int = 3
    backoff_base_s: float = 1.0
    backoff_max_s: float = 30.0
    retry_after_max_s: float = 120.0
    crawl_delay_max_s: float = 60.0
    timeout_s: float = 20.0
    max_redirects: int = 5

    def __post_init__(self) -> None:
        if not 0 <= self.min_delay_s <= self.max_delay_s:
            raise ValueError("need 0 <= min_delay_s <= max_delay_s")
        if not 1 <= self.max_concurrency <= 5:
            raise ValueError("max_concurrency must be between 1 and 5 (compliance cap)")
        if not 0 <= self.max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3 (compliance cap)")
        if self.timeout_s <= 0 or self.backoff_base_s < 0:
            raise ValueError("timeout_s must be > 0 and backoff_base_s >= 0")


def is_retryable(status: int) -> bool:
    """429 Too Many Requests and every 5xx are retried."""
    return status == 429 or 500 <= status <= 599


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date), else ``None``."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


# ---------------------------------------------------------------------------- rate limiting
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class DomainRateLimiter:
    """Serializes requests per host and enforces a jittered gap after each one.

    The gap is measured from the *end* of the previous request (most polite option) and is
    ``max(uniform(min_delay, max_delay), floor)``, where ``floor`` is e.g. a robots ``crawl-delay``.
    """

    def __init__(
        self,
        min_delay_s: float,
        max_delay_s: float,
        *,
        clock: Clock,
        sleep: Sleep,
        rng: random.Random,
    ) -> None:
        self._min, self._max = min_delay_s, max_delay_s
        self._clock, self._sleep, self._rng = clock, sleep, rng
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_at: dict[str, float] = {}
        self._floor: dict[str, float] = {}

    def set_floor(self, host: str, seconds: float) -> None:
        """Raise the minimum gap for ``host`` (never lowers below the jitter range)."""
        self._floor[host] = max(self._floor.get(host, 0.0), seconds)

    def gap(self, host: str) -> float:
        """A fresh jittered gap for ``host``."""
        return max(self._rng.uniform(self._min, self._max), self._floor.get(host, 0.0))

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        """Hold the host's slot: waits for the gap, runs the body, then schedules the next gap."""
        async with self._locks[host]:
            wait = self._next_at.get(host, 0.0) - self._clock()
            if wait > 0:
                await self._sleep(wait)
            try:
                yield
            finally:
                self._next_at[host] = self._clock() + self.gap(host)


# ---------------------------------------------------------------------------- caching
@dataclass(frozen=True, slots=True)
class CachedResponse:
    """What we keep from a 200 response to answer a later ``304 Not Modified``."""

    url: str
    content: bytes
    headers: dict[str, str]
    stored_at: float

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    def to_response(self, request: httpx.Request) -> httpx.Response:
        """Rebuild a ``200`` response (flagged ``from_cache``) for ``request``."""
        return httpx.Response(
            200,
            headers=self.headers,
            content=self.content,
            request=request,
            extensions={FROM_CACHE: True},
        )


class ResponseCache(Protocol):
    """Storage hook for conditional-request validators. Implementations must never raise."""

    def get(self, key: str) -> CachedResponse | None: ...

    def set(self, key: str, entry: CachedResponse) -> None: ...


class InMemoryResponseCache:
    """Process-local cache (tests, one-off runs)."""

    def __init__(self) -> None:
        self._entries: dict[str, CachedResponse] = {}

    def get(self, key: str) -> CachedResponse | None:
        return self._entries.get(key)

    def set(self, key: str, entry: CachedResponse) -> None:
        self._entries[key] = entry


class FileResponseCache:
    """Directory cache that survives between CLI runs (``SCRAPER_HTTP_CACHE_DIR``).

    Each entry is ``<sha256>.json`` (metadata) + ``<sha256>.body``. Corrupt or partial entries are
    treated as misses. Deleting the directory is always safe.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _paths(self, key: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json", self.directory / f"{digest}.body"

    def get(self, key: str) -> CachedResponse | None:
        meta_path, body_path = self._paths(key)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("url") != key:
                return None
            return CachedResponse(
                url=key,
                content=body_path.read_bytes(),
                headers=dict(meta["headers"]),
                stored_at=float(meta["stored_at"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def set(self, key: str, entry: CachedResponse) -> None:
        meta_path, body_path = self._paths(key)
        meta = {k: v for k, v in asdict(entry).items() if k != "content"}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            for path, data in ((body_path, entry.content), (meta_path, json.dumps(meta).encode())):
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_bytes(data)
                tmp.replace(path)  # atomic: readers never see half-written files
        except OSError as exc:
            logger.warning("http.cache_write_failed", extra={"url": key, "error": str(exc)})


def is_from_cache(response: httpx.Response) -> bool:
    """True if ``response`` was served from the cache after a ``304``."""
    return bool(response.extensions.get(FROM_CACHE))


# ---------------------------------------------------------------------------- client
class HttpClient:
    """Async, polite, retrying HTTP client. Use as ``async with HttpClient.from_settings() as h:``.

    Args:
        user_agent: Sent on every request; its product token is used for robots.txt matching.
        policy: Rate-limit / retry settings (defaults = compliance requirements).
        cache: Optional ``ResponseCache`` for ETag / Last-Modified revalidation.
        transport: Optional ``httpx`` transport (``httpx.MockTransport`` in tests).
        clock, sleep, rng: Injectable time and randomness.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        policy: HttpPolicy | None = None,
        cache: ResponseCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        product_token(user_agent)  # validate early
        self.user_agent = user_agent
        self.policy = policy or HttpPolicy()
        self.cache = cache
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._semaphore = asyncio.Semaphore(self.policy.max_concurrency)
        self._limiter = DomainRateLimiter(
            self.policy.min_delay_s,
            self.policy.max_delay_s,
            clock=clock,
            sleep=sleep,
            rng=self._rng,
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=self.policy.timeout_s,
            follow_redirects=True,
            max_redirects=self.policy.max_redirects,
            headers={"User-Agent": user_agent},
        )
        self.robots = RobotsChecker(self._fetch_robots, user_agent, clock=clock)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> Self:
        """Client using ``SCRAPER_USER_AGENT`` and a file cache in ``SCRAPER_HTTP_CACHE_DIR``."""
        settings = settings or Settings.from_env()
        kwargs.setdefault("cache", FileResponseCache(settings.http_cache_dir))
        return cls(user_agent=settings.user_agent, **kwargs)

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -------------------------------------------------------------- public API
    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        respect_robots: bool = True,
        use_cache: bool = True,
    ) -> httpx.Response:
        """GET ``url`` politely.

        Returns the final response for any non-retryable status (2xx, 3xx, 4xx) — callers decide
        what a 404 means. A ``304`` is transparently replaced by the cached ``200``.

        Raises:
            RobotsDisallowedError: robots.txt forbids the URL (unless ``respect_robots=False``).
            FetchError: Retries were exhausted on 429/5xx or transport errors.
        """
        request = self._client.build_request("GET", url, params=params, headers=headers)
        key = str(request.url)
        if respect_robots:
            await self._enforce_robots(key, request.url.host)

        cached = self.cache.get(key) if (use_cache and self.cache) else None
        if cached is not None:
            if cached.etag:
                request.headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                request.headers["If-Modified-Since"] = cached.last_modified

        response = await self._send(request)
        if response.status_code == 304 and cached is not None:
            logger.debug("http.cache_hit", extra={"url": key})
            return cached.to_response(request)
        if use_cache and self.cache and response.status_code == 200:
            self._store(key, response)
        return response

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET and decode JSON. Raises ``FetchError`` on non-2xx or invalid JSON."""
        response = await self._get_ok(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(url, response.status_code, f"invalid JSON: {exc}") from exc

    async def get_text(self, url: str, **kwargs: Any) -> str:
        """GET and return decoded text. Raises ``FetchError`` on non-2xx."""
        return (await self._get_ok(url, **kwargs)).text

    async def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """POST a JSON body to a documented API and decode the JSON reply.

        Same per-host pacing, concurrency cap and retries as ``get``; never cached and never
        robots-checked (only used for third-party APIs such as Firecrawl, never for site pages).

        Raises:
            FetchError: On non-2xx after retries, or an invalid JSON reply.
        """
        request = self._client.build_request("POST", url, json=payload, headers=headers)
        response = await self._send(request)
        if not response.is_success:
            raise FetchError(url, response.status_code, "unexpected HTTP status")
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(url, response.status_code, f"invalid JSON: {exc}") from exc

    # -------------------------------------------------------------- internals
    async def _get_ok(self, url: str, **kwargs: Any) -> httpx.Response:
        response = await self.get(url, **kwargs)
        if not response.is_success:
            raise FetchError(url, response.status_code, "unexpected HTTP status")
        return response

    async def _enforce_robots(self, url: str, host: str) -> None:
        rules = await self.robots.rules_for(url)
        if rules.crawl_delay:
            self._limiter.set_floor(host, min(rules.crawl_delay, self.policy.crawl_delay_max_s))
        if not rules.is_allowed(url):
            logger.info("http.robots_disallowed", extra={"url": url})
            raise RobotsDisallowedError(url)

    async def _fetch_robots(self, robots_url: str) -> RobotsFetch:
        request = self._client.build_request("GET", robots_url)
        try:
            response = await self._send(request)
        except FetchError as exc:
            return RobotsFetch(status=exc.status)
        return RobotsFetch(status=response.status_code, content=response.content)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        """Send with per-host serialization, global concurrency cap and retries."""
        url, host = str(request.url), request.url.host
        async with self._limiter.slot(host):
            for attempt in range(self.policy.max_retries + 1):
                response: httpx.Response | None = None
                error: httpx.TransportError | None = None
                started = time.perf_counter()
                async with self._semaphore:
                    try:
                        response = await self._client.send(request)
                    except httpx.TransportError as exc:
                        error = exc
                elapsed_ms = round((time.perf_counter() - started) * 1000)

                if response is not None and not is_retryable(response.status_code):
                    logger.debug(
                        "http.response",
                        extra={"url": url, "status": response.status_code, "ms": elapsed_ms},
                    )
                    return response

                status = response.status_code if response is not None else None
                if attempt == self.policy.max_retries:
                    reason = f"gave up after {attempt + 1} attempts"
                    if error is not None:
                        raise FetchError(url, None, f"{reason}: {error!r}") from error
                    raise FetchError(url, status, reason)

                delay = self._retry_delay(attempt, response, host)
                logger.warning(
                    "http.retry",
                    extra={
                        "url": url,
                        "attempt": attempt + 1,
                        "status": status,
                        "error": repr(error) if error else None,
                        "delay_s": round(delay, 2),
                    },
                )
                await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _retry_delay(self, attempt: int, response: httpx.Response | None, host: str) -> float:
        policy = self.policy
        backoff = min(policy.backoff_max_s, policy.backoff_base_s * 2**attempt)
        backoff += self._rng.uniform(0, policy.backoff_base_s)  # jitter avoids thundering herds
        retry_after = parse_retry_after(response.headers.get("Retry-After")) if response else None
        if retry_after is not None:
            backoff = max(backoff, min(retry_after, policy.retry_after_max_s))
        return max(backoff, self._limiter.gap(host))

    def _store(self, key: str, response: httpx.Response) -> None:
        headers = {h: response.headers[h] for h in _CACHED_HEADERS if h in response.headers}
        if "etag" not in headers and "last-modified" not in headers:
            return  # nothing to revalidate with
        self.cache.set(  # type: ignore[union-attr]  # guarded by caller
            key,
            CachedResponse(
                url=key, content=response.content, headers=headers, stored_at=time.time()
            ),
        )
