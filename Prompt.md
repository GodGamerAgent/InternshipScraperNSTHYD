# Kickoff Prompt for Claude (Phase 1)

Copy and paste the prompt below once `Implementation.md` and `Context.md` are saved in your project root:

---

You are a Senior Python Data Engineer and Software Architect. We are building an **India-optimized Startup Internship & Hidden-Opportunity Scraper** with city-level geolocation filtering (Hyderabad, Bengaluru, Chennai, Mumbai, Delhi NCR, Pune, plus Remote India) and ethical public contact discovery.

Read `Context.md` and `Implementation.md` in the project root carefully before writing any code. They are the source of truth for our product boundaries, scraping methods, data models, geolocation rules, and compliance guardrails.

### Mandatory Pre-Flight Check & Multi-AI Handoff Protocol
**Before writing any code or running any commands**, you must first state clearly in your opening reply:
1. **Feasibility Check:** Whether you can complete the requested phase/task cleanly (or flag any missing tools, permissions, or blockers upfront).
2. **Usage-Limit Visibility:** Explicitly tell me whether you can see your remaining account usage limit or context window. Do **not** guess or pretend you can see usage limits if you cannot.

Then follow these continuity rules throughout every session:
* **If you can detect that your usage limit or context window is close to running out:** Stop at the nearest clean, passing checkpoint (never leave half-edited or broken files), update `PROGRESS.md`, and give me a self-contained **Handoff Prompt** so the next AI can pick up from your exact stopping point.
* **Always maintain `PROGRESS.md` (especially if you cannot see usage limits):** Create `PROGRESS.md` in the project root and update it **after every completed sub-step**—not just at the end of a phase—so another AI can resume seamlessly even if the session cuts off unexpectedly. Keep `PROGRESS.md` updated with:
  * Completed checklist items and exact file paths created/modified.
  * Last verified `ruff check` and `pytest` results.
  * Current in-progress step and exact next step to run.
  * A ready-to-paste **Resume Prompt** for the next AI session.

### Non-Negotiable Engineering Rules
1. **No Monolithic Scripts:** Keep every layer strictly separated (`src/core/`, `src/models/`, `src/collectors/`, `src/enrichment/`, `src/storage/`, `src/cli.py`). Every collector must inherit from `BaseCollector` and never write directly to SQLite.
2. **Modern Typed Python:** Use Python 3.12+, `uv` for dependency management, `Pydantic v2` for strict data contracts, `httpx` (async), `BeautifulSoup4`/`lxml`, `Typer` for the CLI, `Ruff` for linting/formatting, and `pytest` for testing.
3. **Config Over Hardcoding:** Store city aliases, PIN code prefixes, and coordinates in `config/india_geo.yaml`, seed company lists and internship regexes in `config/sources.yaml`, and hidden-opportunity weights in `config/scoring.yaml`.
4. **Compliance First:** Never scrape login-walled social networks or bypass CAPTCHAs. Enforce `robots.txt` compliance (RFC 9309), per-domain jittered rate limits, and DNS MX-only email domain checks (no aggressive SMTP probing).
5. **Real-Dev Documentation:** Write clean docstrings, type hints, `AGENTS.md`, `README.md`, `docs/ARCHITECTURE.md`, and `docs/ADRs/001-stack-and-storage.md` so a human engineer joining later can clone, test, and add a new collector in under 15 minutes.
6. **Offline Tests Required:** Every parser and enrichment module must have unit tests using saved local fixtures under `tests/fixtures/` so `pytest` passes completely offline.

### Your Task Right Now: Execute Phase 1 Only
Do **not** build the scrapers yet. After your upfront feasibility & usage-limit check, complete **Phase 1 — Foundation & Geo Engine** cleanly (updating `PROGRESS.md` after each numbered step) and stop for my review:

1. Initialize the `uv` project and `pyproject.toml` with dependencies (`httpx`, `beautifulsoup4`, `lxml`, `pydantic`, `sqlmodel`, `typer`, `pyyaml`, `dnspython`, `feedparser`) and dev dependencies (`pytest`, `pytest-asyncio`, `ruff`).
2. Create the full directory structure (`src/`, `config/`, `docs/ADRs/`, `tests/fixtures/`) plus `.env.example`, `.gitignore`, and `PROGRESS.md`.
3. Create `AGENTS.md` (strict rules for future AI coding sessions), `README.md` (5-minute setup + CLI usage), `docs/ARCHITECTURE.md` (pipeline + how to add a new collector), and `docs/ADRs/001-stack-and-storage.md`.
4. Create `config/india_geo.yaml`, `config/sources.yaml`, and `config/scoring.yaml` with complete India city aliases (Hyderabad, Bengaluru, Chennai, Mumbai, Delhi NCR, Pune, Kolkata, Ahmedabad), tech-hub neighborhoods (e.g., HITEC City, Gachibowli, Koramangala, OMR), PIN code prefix mappings, and city centroids.
5. Implement `src/models/` (`Startup`, `Opportunity`, `Contact` Pydantic v2 models with canonical location and India stipend fields).
6. Implement `src/storage/` (SQLite repository with idempotent upsert/deduplication on `canonical_domain`, opportunity hash, and contact email/profile URL).
7. Implement `src/enrichment/geo.py` (3-step location resolver: alias/neighborhood match -> Remote-India detection -> PIN code / HQ fallback).
8. Write unit tests in `tests/test_geo.py` and `tests/test_storage.py`, then run `uv run ruff check .` and `uv run pytest` and show me the exact command output.

When Phase 1 passes all lint and test checks, update `PROGRESS.md` with a final Phase 1 status + Phase 2 resume prompt, summarize what you created, and wait for my confirmation before starting Phase 2.

---

# Follow-Up Prompts (Run One Phase Per Session)

### Phase 2 — Core HTTP, Rate Limiting & `BaseCollector`
> Phase 1 looks good. Now implement **Phase 2** from `Implementation.md`:
> 1. Build `src/core/http.py`: async `httpx` client wrapper with per-domain jittered rate limiting (1.5–2.5s), global concurrency cap (5), exponential backoff retries on 429/5xx, `ETag`/`If-Modified-Since` caching hooks, and a transparent `User-Agent`.
> 2. Build `src/core/robots.py`: cached `robots.txt` checker compliant with RFC 9309.
> 3. Build `src/core/base.py`: abstract `BaseCollector` interface (`fetch()`, `parse()`, `run()`) with structured logging and fault-isolation boundaries.
> 4. Add offline unit tests mocking HTTP responses and `robots.txt` rules. Run `uv run ruff check .` and `uv run pytest`, then stop for review.

### Phase 3 — Tier 1 Public ATS Collectors (India-Filtered)
> Now implement **Phase 3** from `Implementation.md`:
> 1. Build isolated collectors in `src/collectors/` for **Greenhouse**, **Lever**, **Ashby**, and **Workable** using the exact public JSON endpoints in `Implementation.md`.
> 2. Add India internship keyword matching, INR stipend/duration/PPO extraction (`src/enrichment/stipend.py`), and pipe every result through `geo.py` and the SQLite repository.
> 3. Add realistic anonymized JSON fixtures in `tests/fixtures/` for each ATS and write offline parser tests.
> 4. Seed `config/sources.yaml` with 10–15 well-known Indian tech startups that use these ATS boards. Run `uv run ruff check .` and `uv run pytest`, then stop for review.

### Phase 4 — Hidden Opportunities, India Directories, Contacts & Optional Firecrawl
> Now implement **Phase 4** from `Implementation.md`:
> 1. Add collectors for **T-Hub** (Hyderabad), **Inc42 RSS**, **Entrackr funding**, and **HN Who is Hiring (India/IST/Remote filter)**.
> 2. Build `src/enrichment/ats_discovery.py` (probes `/careers`, `/jobs`, `/join` for ATS embeds and cold-pitch invitations), `src/enrichment/github_signals.py` (GitHub REST API org velocity), and `src/enrichment/scorer.py` (0–100 hidden-opportunity rubric from `config/scoring.yaml`).
> 3. Build `src/enrichment/contacts.py` implementing the 3-pass public contact pipeline (`direct_public`, `github_public`, `pattern_mx_verified` using `dnspython` MX checks only) with confidence scores.
> 4. Add an optional Tier 3 fallback client (`src/core/firecrawl_client.py`) activated only when `FIRECRAWL_API_KEY` is set in `.env`, used strictly for JS-heavy careers/about pages when plain `httpx` returns empty or SPA-only HTML.
> 5. Add offline HTML/JSON fixtures and unit tests for every module. Run `uv run ruff check .` and `uv run pytest`, then stop for review.

### Phase 5 — Geolocation CLI, Exports & Daily Automation
> Now implement **Phase 5** from `Implementation.md`:
> 1. Wire `src/cli.py` with `Typer` commands: `scrape`, `enrich`, `export`, and `stats`. Support `--cities` (e.g., `hyderabad,bengaluru,chennai`), `--include-remote`, `--work-mode`, `--min-hidden-score`, and `--format csv|json`.
> 2. Add a GitHub Actions workflow (`.github/workflows/daily_scrape.yml`) to run daily on a schedule and upload CSV artifacts.
> 3. Update `README.md` and `docs/ARCHITECTURE.md` to reflect the exact final CLI flags and project structure.
> 4. Run full `uv run ruff check .`, `uv run pytest`, and a dry-run CLI check, then present a final handoff checklist for future developers.
