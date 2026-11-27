# ADR 006 — Optional Local NiceGUI Dashboard

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair (Phase 6)
- **Supersedes:** a brief Streamlit attempt (reverted before any UI code was written)

## Context
Students want to browse internships, hidden opportunities and contacts without spreadsheets, but
the project is CLI-first and zero-infrastructure (ADR 001), CI must stay light, and the data
(including public contacts) is personal to whoever runs it.

## Decisions

### 1. NiceGUI as an optional extra
`[project.optional-dependencies] ui = ["nicegui>=2.0"]` (4.1.0 at time of writing). Default
`uv sync` and CI (`uv sync --frozen`, which is exact and removes extras) never install it.
Install with `uv sync --extra ui`; plain `uv run` keeps an installed extra. NiceGUI was chosen
over Streamlit for event-driven updates (no full-script reruns), native `async` handlers for the
long-running scrape/enrich jobs, and Quasar tables with slots for badges and links.

### 2. Thin UI layer with an enforced boundary
- `src/ui/view_models.py` — pure functions: `UiFilters → ExportFilter`, rows for each tab, labels
  (Indian digit grouping `₹1,50,000`), KPI cards, CSV/JSON payloads via `exports.rows_to_csv` /
  `rows_to_json` (formula-safe). No NiceGUI import; fully tested offline.
- `src/ui/jobs.py` — `scrape_job` / `enrich_job` wrap `pipeline.run_scrape` / `enrich_all`, stream
  INFO log events to the page, never raise, and serialize runs with `JOB_LOCK`.
- `src/ui/app.py` — the only module importing `nicegui` (layout and events).
- `tests/test_ui_helpers.py` fails if any `src/ui` module imports `httpx`, `sqlalchemy`, `sqlmodel`,
  `sqlite3` or `src.storage.{tables,db,repository}`, or if anything outside `src/ui/app.py` imports
  `nicegui`.

### 3. Private and read-only by default
- **Bind address:** NiceGUI's `ui.run` defaults to `0.0.0.0` in normal mode. `run_ui` always passes
  the host explicitly (default `127.0.0.1`) and `reload=False`. `scraper ui` refuses wildcard hosts
  (`0.0.0.0`, `::`, empty) and warns for any other non-loopback address.
- **No network on load:** the page reads only the local SQLite file. Network access happens only via
  the explicit **Run scrape / Run enrich** buttons, through the same pipeline as the CLI (robots.txt,
  rate limits, retries, MX-only contact checks). Verified in the browser: every request on load went
  to the local server (NiceGUI bundles its assets and fonts).
- **Links:** only `http(s)` URLs from validated models (`HttpUrlStr`) or built from validated slugs
  are rendered as `href`; Vue's `{{ }}` escapes all scraped text.

### 4. UX choices
- Theme toggle **Auto / Light / Dark** (Auto follows the OS). An explicit page background is set
  because Quasar's light body is transparent: without it, a dark browser canvas showed through
  and made the tab labels invisible (found during the visual check).
- Work-mode filter offers all four modes (`remote_global` included, so worldwide HN roles can be
  selected); paid/PPO toggles apply to internships only.
- Pattern-guessed contacts (0.50) carry an "Unconfirmed guess" badge and note everywhere.

## Consequences
- 39 UI tests run in CI without NiceGUI; one smoke test (`run_ui` passes `127.0.0.1`, `reload=False`)
  runs only when the extra is installed.
- The UI is single-user and local; exposing it on a network is a deliberate, warned choice and has no
  authentication — a future ADR would be needed for any shared deployment.
