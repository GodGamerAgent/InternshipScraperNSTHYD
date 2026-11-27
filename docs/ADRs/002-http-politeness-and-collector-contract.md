# ADR 002 — HTTP Politeness Layer and Collector Contract

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair (Phase 2)
- **Amends:** the provisional `fetch()` / `parse(payload)` sketch in `docs/ARCHITECTURE.md` (Phase 1)

## Context
Phase 2 had to turn the compliance rules in `implementation.md` §5 (robots.txt per RFC 9309,
1.5–2.5 s per-domain jitter, ≤ 5 concurrent, ≤ 3 retries on 429/5xx, ETag/If-Modified-Since,
transparent UA) into code that every collector uses, and fix the collector interface before
Phase 3 writes four collectors against it.

## Decisions

### 1. Own RFC 9309 parser instead of `urllib.robotparser`
The stdlib parser applies the first matching rule and ignores `*` / `$`. RFC 9309 requires
longest-match (ties → allow) and wildcards; real startup sites (WordPress, Next.js) use both. The
parser is ~150 lines, pure, and covered by fixture tests. Interpretation choices:
- **429 is treated as "unreachable" (disallow all)**, although RFC 9309 groups it with 4xx
  ("unavailable", allow all). A rate-limited robots.txt is a signal to back off, not to crawl.
- Unreachable results are cached for 15 min (vs 24 h) so a brief outage does not block a whole run.

### 2. robots.txt is checked by default; documented JSON APIs may opt out
`HttpClient.get(..., respect_robots=True)` is the default. Collectors for **documented public
APIs** (Greenhouse, Lever, Ashby, Workable widget, HN Algolia, GitHub REST) may pass
`respect_robots=False` with a call-site comment: these endpoints are published for programmatic
access, and some API hosts serve a blanket `Disallow: /` aimed at web crawlers. HTML pages
(careers, about, contact, directories) must never opt out.

### 3. Pacing: one in-flight request per host, gap measured from completion
`DomainRateLimiter` holds a per-host lock for the whole request (including retries), then schedules
the next slot `uniform(1.5, 2.5)` s after it finished (raised to the robots `crawl-delay`, capped at
60 s). This is stricter than spacing request *starts* and means a slow server automatically gets
fewer requests. The global `Semaphore(5)` is acquired *inside* the host slot and released during
backoff sleeps, so a host that is backing off never holds a global slot.

### 4. Compliance caps are enforced in code, not config
`HttpPolicy` rejects `max_concurrency > 5` and `max_retries > 3`. Delays stay configurable because
tests use an injected fake clock instead of lowering them. Rationale: politeness minimums should not
be one YAML edit away from being disabled.

### 5. Errors: 4xx returned, 429/5xx retried, then `FetchError`
`get()` returns any non-retryable response (callers decide what a 404 board means);
`get_json()` / `get_text()` raise `FetchError` on non-2xx. `RobotsDisallowedError` subclasses
`FetchError` and must never be retried or worked around.

### 6. Persistent conditional-request cache
`FileResponseCache` (`SCRAPER_HTTP_CACHE_DIR`, default `data/http_cache/`) stores only responses
carrying `ETag`/`Last-Modified`, so daily CLI / GitHub Actions runs can revalidate cheaply. Only
`content-type`, `etag`, `last-modified` headers are kept (never `content-encoding`, since the body
is stored decoded). An in-memory implementation exists for tests; both satisfy `ResponseCache`.

### 7. Collector contract: `targets()` → `fetch(target)` → `parse(raw, target)`
The Phase 1 sketch (`fetch() -> list[payload]`) gave no per-target isolation: one bad company slug
would fail the whole Greenhouse collector. The final contract is
`BaseCollector[TargetT, RawT, ItemT]` where `run()` (inherited, never raises) fetches every target
concurrently, each inside its own `try` + `asyncio.timeout(target_timeout_s)`, and records failures
as `CollectorIssue(stage, target, error_type, message, status)`. `parse()` receives the target too,
because building a `Startup` usually needs the slug/domain. `run_all()` adds the same boundary around
whole collectors.

### 8. Structured logging via stdlib
Event-name messages plus `extra={...}` fields (`logger.info("http.retry", extra={...})`), rendered by
`KeyValueFormatter` (default) or `JsonFormatter` (`SCRAPER_LOG_FORMAT=json`). No new dependency.

## Consequences
- All network behaviour is testable offline (`httpx.MockTransport` + `FakeTime`); 222 tests run in < 1 s.
- Throughput is deliberately low (≈ 0.5 req/s per host). Phase 3 boards are one request per company,
  so a 15-company run takes well under a minute.
- Cross-domain redirects are followed (max 5) without re-checking robots for the new host; acceptable
  for ATS APIs, revisit in Phase 4 if careers-page crawls hit redirect chains.
- `core` stays dependency-free of higher layers (`src/core` imports only `src/core`).
