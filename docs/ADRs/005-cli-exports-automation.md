# ADR 005 — CLI, Exports, Posting Lifecycle and Daily Automation

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair (Phase 5)

## Context
Phase 5 exposes the pipeline as a CLI (`scrape`, `enrich`, `export`, `stats`), closes postings that
disappear, and runs everything daily on GitHub Actions — while keeping `src/cli.py` free of
business logic and the suite fully offline.

## Decisions

### 1. Thin CLI, logic in `pipeline.py` / `exports.py` / `storage`
`src/cli.py` parses and validates flags (cities against `india_geo.yaml`, sources against
`ALL_COLLECTOR_NAMES`, work modes against `WorkMode`; bad values exit 2) and prints tables.
`run_scrape`, `plan_scrape`, `select_for_enrichment` live in `pipeline.py`; rows/CSV/JSON/stats in
the new `src/exports.py`; filtering in `Repository.query_*` with `ExportFilter`. Network and DNS are
built by `cli.make_http_client` / `cli.make_mx_resolver`, which tests replace.

### 2. Closing vanished postings is scoped to boards that succeeded
`BaseCollector.close_scope(target)` (ATS: seed domain; HN: `"*"`; leads collectors: `None`).
Only boards fetched **and** parsed without an issue are eligible, and a result with any persistence
error closes nothing. False closures (a board that timed out) would silently hide live internships,
which is worse than a stale row for one more day. Reappearing postings are reopened by the upsert.

### 3. Export semantics
`(city AND work_mode) OR (include_remote AND remote_india)`; `min_hidden_score` filters on the
startup's score and drops startup-less rows (HN postings without a domain); default `status=open`.
Column sets are fixed per export kind. CSV cells beginning with `= + - @` (or tab/CR) get a leading
`'` — scraped text must never execute as a spreadsheet formula.

### 4. `scrape --dry-run`
Lists collectors and targets (YAML seeds + boards auto-discovered in the DB) with no network and no
database creation. CI prints it before the real run; humans use it to check `--sources/--cities`.

### 5. Exit codes
`scrape` exits 1 only if *every* target failed; `enrich` exits 1 only if every startup failed.
Individual source failures are expected on a daily run and are printed as warnings, so one flaky
site does not mark the whole workflow red.

### 6. Daily workflow (`.github/workflows/daily_scrape.yml`)
- 01:20 UTC = 06:50 IST (off the top of the hour, where scheduled jobs queue), plus manual dispatch.
- `uv sync --frozen`, then ruff + pytest gate the run.
- `data/` (SQLite DB + ETag cache) is restored/saved with `actions/cache/{restore,save}` keyed by run
  id, so dedup, closing and conditional requests work across days. (Caches unused for 7 days are
  evicted by GitHub; the first run after that simply starts from an empty DB.)
- Secrets only through `env:`; no `${{ }}` is interpolated into shell commands (injection-safe).
  Custom secret names cannot start with `GITHUB_`, so an optional PAT is `SCRAPER_GITHUB_TOKEN`,
  falling back to the built-in token.
- `exports/` uploaded as an artifact for 14 days. **Contacts are not exported unless
  `EXPORT_CONTACTS=true`** (or the dispatch input): artifacts of a public repository are
  downloadable by anyone with read access.
- `permissions: contents: read`; `concurrency` prevents overlapping runs writing the DB.

## Consequences
- 452+ offline tests including `CliRunner` end-to-end flows and static workflow checks
  (`tests/test_workflow.py`); the workflow itself is unverified until the repo is pushed to GitHub.
- The Actions cache is not a durable database; long-term history would need a release asset or
  external storage (future ADR).
