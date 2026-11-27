# AGENTS.md — Rules for Every AI Coding Session

These rules are **mandatory** for any AI assistant (Claude, Codex, Copilot, Cursor, …) or human
editing this repository. If a request conflicts with a rule here, stop and ask the user.

## 0. Read first, every session
1. `context.md` — product vision, India domain nuances, in/out of scope.
2. `implementation.md` — exact endpoints, schemas, geo rules, guardrails.
3. `PROGRESS.md` — what is done, what is next, last verified `ruff`/`pytest` result.
4. This file.

Work **one phase at a time** (phases are defined in `implementation.md` §6 and `Prompt.md`).
Never start the next phase without explicit user confirmation.

## 1. Continuity protocol (non-negotiable)
- Update `PROGRESS.md` **after every completed sub-step**: checklist status, files created/modified,
  last `ruff`/`pytest` result, current step, exact next step, and a ready-to-paste Resume Prompt.
- If you can detect your context/usage is running out: stop at the nearest **green** checkpoint
  (no half-edited files), update `PROGRESS.md`, and print a self-contained Handoff Prompt.
- Never claim to see usage limits you cannot see.

## 2. Architecture boundaries
```
src/core/         infrastructure: config loading, URL helpers, HTTP client, robots.txt, BaseCollector
src/models/       Pydantic v2 data contracts — the ONLY shapes that cross layer boundaries
src/collectors/   one module per source, each a BaseCollector subclass; returns models, never writes DB
src/enrichment/   pure-ish transforms: geo, stipend, scorer, contacts, ats_discovery, github_signals
src/storage/      SQLite (SQLModel/SQLAlchemy Core) repository; the ONLY code that touches the DB
src/pipeline.py   scrape/enrich orchestration: runs collectors, persists via Repository, closes
                  vanished postings; the only module that combines collectors/enrichment with storage
src/exports.py    export rows, CSV/JSON rendering (formula-safe), stats summary
src/cli.py        Typer wiring only — parse/validate flags, call pipeline/exports, print
src/ui/           optional NiceGUI dashboard: app.py (only nicegui importer) · view_models.py · jobs.py
```
Dependency direction (an arrow means "may import"):
`cli → {pipeline, exports} → {collectors, storage}`, `collectors → enrichment → models → core`,
`storage → models → core`. Lower layers never import higher ones: `core` imports only `core`;
`models` may import `src.core.urls`/`src.core.text` only; `enrichment` never imports `collectors`
(use a `Protocol`, see `InternshipRules`); only `src/pipeline.py` combines collectors with storage.
`ui → {pipeline, exports, storage.Repository/ExportFilter, enrichment, models}`; `src/ui` never
imports `httpx`, SQLAlchemy/SQLModel, `sqlite3` or `src.storage.{tables,db,repository}`, and nothing
outside `src/ui/app.py` imports `nicegui` (optional extra; CI does not install it).

- **No monolithic scripts.** A new source = a new file in `src/collectors/` + fixture + test.
- **Collectors never write to SQLite.** They yield validated models; the pipeline calls the repository.
- **Seeds must be verified** against the live public API before being added to `sources.yaml`
  (record `verified_on`). Never guess board slugs or HQ cities; omit `hq_city` when unsure.
- **Automated discovery never writes `sources.yaml`**: probed / searched boards go to SQLite only,
  must pass `ats_probe.board_matches` (domain link or exact name — never prefix matching), and use
  only official search APIs (Brave, Firecrawl) — never scrape search-engine HTML (ADR 007).
- **HTML-page ATS portals** (Zoho Recruit) respect robots.txt. Zoho seed slugs must be full
  `*.zohorecruit.(in|com)` hosts. A portal whose job data is robots-disallowed (Keka) stays
  detection-only. Directories (YC India, IITM, NSRCEL) emit `StartupLead`s, and an incubator's
  `kind` picks exactly one collector (ADR 008).
- Collectors implement `targets()` / `fetch(target)` / pure `parse(raw, target)` and inherit `run()`
  (see `docs/ARCHITECTURE.md` §6). Do not override `run()`.
- **Every record passes through a Pydantic model** before it reaches storage.

## 3. Config over hardcoding
| What | Where |
|---|---|
| City aliases, neighborhoods, PIN prefixes, centroids, remote/hybrid phrases | `config/india_geo.yaml` |
| Seed ATS slugs, RSS feeds, incubators, internship regexes, careers paths | `config/sources.yaml` |
| Hidden-opportunity weights, thresholds, contact confidence values | `config/scoring.yaml` |

If you are about to type a city name, company slug, regex for job titles, or a scoring number into a
`.py` file, put it in YAML instead. Config is validated by Pydantic at load time — extend the schema
when you add keys.

## 4. Compliance guardrails (never violate)
- **No login-walled social networks** (LinkedIn, private X/Twitter). **No CAPTCHA bypassing.**
- Respect `robots.txt` (RFC 9309) before fetching any HTML page. `HttpClient.get()` does this by
  default; `respect_robots=False` is allowed **only** for documented public JSON APIs (the four Tier 1 ATS boards,
  HN Algolia, GitHub REST) and needs a comment at the call site (ADR 002). ATS providers added later
  keep robots on (`AtsCollector.respect_robots = True`, ADR 009). Never catch
  `RobotsDisallowedError` to retry another way.
- Per-domain jittered delay 1.5–2.5 s, global concurrency ≤ 5, ≤ 3 exponential-backoff retries on 429/5xx.
- Transparent `User-Agent` from `SCRAPER_USER_AGENT`.
- Email checks are **DNS MX lookups only** (`dnspython`). Never SMTP `RCPT TO` probing.
- Only collect contacts that are **publicly listed for business/recruiting**. No personal phone numbers.
  Emails must be on the company's own domain (personal webmail is never stored, even if published);
  non-recruiting mailboxes (press@, sales@, …) are excluded; profile sites are linked, never fetched.
- **Never store job-seeker data.** HN "Who is hiring?" threads contain seeker posts; skip them
  (`hacker_news.seeker_patterns`). Pattern-guessed emails require a DNS MX record first.
- Student portals (ADR 011): Internshala, Cutshort and Instahyre are collected **by owner decision**, even
  though their Terms prohibit automated access. Only public listing pages are fetched, never detail,
  apply or login pages, and recruiter personal data is never read. A 403 or challenge (Unstop) is a
  non-target and is never worked around.
- Firecrawl (Tier 3) is optional and only for JS-only pages; check the target's robots.txt with our
  own User-Agent before delegating (`FirecrawlClient.scrape_html` does this).
- No automated outreach (no email sending, no DM bots). Output is for human-reviewed outreach.
- The local dashboard binds `127.0.0.1` by default and must never default to `0.0.0.0`; loading a
  page must not trigger network calls (only explicit run buttons may), ADR 006.

## 5. Code style
- Python **3.12+**, full type hints, `from __future__` not needed. Modern syntax (`X | None`, `list[str]`).
- Pydantic v2 (`model_validator`, `field_validator`, `ConfigDict`). No Pydantic v1 APIs.
- Timezone-aware UTC datetimes only (`datetime.now(UTC)`); Ruff `DTZ` enforces this.
- All network access goes through `src.core.http.HttpClient` (`self.http` in collectors) — never
  import `httpx` in `src/collectors/` or `src/enrichment/`. Don't lower `HttpPolicy` limits.
- Docstrings on every public module, class, and function: *what* and *why*, not a restatement of the code.
- Use `logging.getLogger(__name__)` with event-style messages + `extra`:
  `logger.info("collector.finished", extra={"items": 3})`. No `print()` outside `src/cli.py`.

## 6. Testing (offline only)
- Every parser and enrichment module gets unit tests driven by fixtures in `tests/fixtures/`
  (anonymized JSON/HTML — no real personal emails/names).
- **`pytest` must pass with the network unplugged.** Mock HTTP; never hit live endpoints in tests.
- Storage tests use a temp SQLite file (`tmp_path`), never `data/`.

## 7. Definition of done (every sub-step)
```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
All three green → update `PROGRESS.md` → (if a public interface or CLI flag changed) update
`README.md` / `docs/ARCHITECTURE.md` / add an ADR under `docs/ADRs/`. A new CLI flag also needs a
`tests/test_cli.py` case, and a flag used in `.github/workflows/daily_scrape.yml` must exist
(`tests/test_workflow.py` checks this).

## 8. Things that look tempting but are forbidden
- Adding Playwright/Selenium without an ADR (Tier 3 is Firecrawl-optional by design).
- Swallowing exceptions silently. Collectors catch at the `run()` boundary, **log**, and continue.
- Renaming deduplication keys or DB columns without a migration note in an ADR.
- Committing `.env`, `data/`, or exported CSVs.
