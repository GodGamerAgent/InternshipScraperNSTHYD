# ADR 001 — Language, Stack, and Storage

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair (Phase 1)

## Context
We need a maintainable scraper that a student can run on a laptop or a free GitHub Actions runner,
that many AI sessions will extend incrementally, and that a human engineer can take over quickly.
Requirements: polite async HTTP, HTML/RSS parsing, strict data validation, local persistence with
deduplication, a CLI, and an offline test suite.

## Decision

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Best scraping/parsing ecosystem; modern typing (`X \| None`, `StrEnum`). |
| Env & deps | `uv` + `uv.lock` | Fast, reproducible, manages the Python interpreter too. |
| Data contracts | Pydantic v2 | Validates every record at layer boundaries; clear errors for AI-written parsers. |
| HTTP | `httpx` (async) | HTTP/1.1+2, timeouts, transport mocking for offline tests. |
| Parsing | BeautifulSoup4 + `lxml`, `feedparser` | Robust against messy HTML; standard RSS/Atom handling. |
| DNS | `dnspython` | MX-only email domain checks (no SMTP probing — compliance). |
| Storage | SQLite via SQLModel (tables) + SQLAlchemy Core (upserts) | Zero-infra, single file, artifact-friendly in CI; native `ON CONFLICT` upserts. |
| CLI | Typer | Typed, self-documenting flags. |
| Lint/format | Ruff | One fast tool for lint + format + import sorting. |
| Tests | pytest + pytest-asyncio | Offline fixtures; async collector tests. |

### Package layout: the import package is literally `src`
The spec fixes paths such as `src/core/` and `src/cli.py`, so the package is imported as
`from src.models import Startup` and the console script is `scraper = "src.cli:app"`. This is
non-standard (most projects use `src/<pkg>/`), but it keeps every spec, prompt, and doc path exact.
Renaming later is mechanical (`src.` → `<pkg>.`) and should be its own ADR.

### Domain models vs. table models
Pydantic models in `src/models/` are the public contract. SQLModel table classes in
`src/storage/tables.py` are a private persistence detail; the repository converts between them.
This keeps collectors and enrichment free of ORM concerns and lets the schema evolve independently.

### Deduplication keys
- Startup: `canonical_domain` (lowercased host, `www.` stripped, port removed).
- Opportunity: `fingerprint = sha256(canonical_domain | normalized_title | normalized_url)`.
- Contact: UNIQUE(`startup_id`, `dedupe_key`), `dedupe_key = "email:<lower>"` else `"url:<profile>"`.

Upserts use `COALESCE(excluded.x, x)` — new non-null values win, missing values never erase data.
Known trade-off: a contact first found by profile URL and later by email produces two rows; merging
is deferred to Phase 4 contact enrichment.

## Alternatives considered
- **Postgres / Supabase:** better concurrency, but needs infrastructure; contradicts zero-infra v1.
  The repository interface keeps a later swap localized to `src/storage/`.
- **Scrapy:** powerful, but its framework conventions fight our tiered API-first design and make
  per-source unit testing heavier than plain `httpx` + pure `parse()` functions.
- **Playwright by default:** heavy, slow, flaky in CI. Tier 3 uses optional Firecrawl instead.
- **Plain `sqlite3`:** fewer deps, but SQLModel gives typed table definitions and SQLAlchemy's
  dialect-level upsert builder with no extra cost.

## Consequences
- Anyone with `uv` can `uv sync && uv run pytest` in minutes.
- SQLite limits concurrent writers to one; the pipeline writes from a single orchestrator task.
- Naive datetimes read back from SQLite are treated as UTC by model validators.
