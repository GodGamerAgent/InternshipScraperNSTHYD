# Implementation Plan: India Startup Internship & Hidden-Opportunity Scraper

This document defines the scraping methods, India-focused data sources, geolocation resolution system, contact extraction workflow, and engineering guardrails for the project. It serves as the reference spec for both AI-assisted coding sessions and future engineers maintaining the codebase.

## 1. Core Architecture & Scraping Philosophy

Every data source runs as an isolated plugin behind a shared `BaseCollector` contract so a single broken website never stops the pipeline.

### Tiered Acquisition Strategy (API-First)

Always use the lowest-friction, most stable acquisition method available for a target:

| Tier | Method | Tools | When to Use |
| :--- | :--- | :--- | :--- |
| **Tier 1** | Public JSON / REST APIs | `httpx` | ATS job boards (Greenhouse, Lever, Ashby, Workable), Hacker News Algolia API, GitHub REST API. |
| **Tier 2** | RSS/Atom & Static HTML | `httpx` + `BeautifulSoup4` / `lxml` | Indian funding news feeds (Inc42, Entrackr), city incubator directories (T-Hub), company `/careers` and `/about` pages. |
| **Tier 3** | JS Rendering / AI Extraction Fallback | `Playwright` or optional [Firecrawl](https://www.firecrawl.dev/) (`FIRECRAWL_API_KEY`) | Only for JavaScript-rendered startup directories or Indian ATS portals with no public JSON endpoint. |

### Explicit Non-Targets (Do Not Scrape)

* **Login-walled social networks** (e.g., authenticated LinkedIn feeds or private X endpoints) due to strict terms of service, account bans, and fragile DOM changes.
* **CAPTCHA-protected aggregators** where anti-bot bypasses create ongoing maintenance debt and legal risk.
* **Private personal data** (personal phone numbers or private email addresses not publicly listed for recruiting).

## 2. India Geolocation & City-Filtering Engine

Indian startup listings use inconsistent city names, neighborhood names, and remote labels. A dedicated geolocation module (`src/enrichment/geo.py`) normalizes every startup HQ and job posting into structured location fields before saving to SQLite.

### Canonical Location Schema
Every `Startup` and `Opportunity` stores:
* `country`: `"IN"` or `"GLOBAL"`
* `city_canonical`: Canonical slug (`hyderabad`, `bengaluru`, `chennai`, `mumbai`, `delhi_ncr`, `pune`, `kolkata`, `ahmedabad`, `remote_india`, `unknown`)
* `state`: Canonical state name (e.g., `Telangana`, `Karnataka`, `Tamil Nadu`, `Maharashtra`)
* `work_mode`: `onsite`, `hybrid`, `remote_india`, or `remote_global`
* `lat` / `lng`: City centroid coordinates (enables radius queries such as "within 50 km of Hyderabad")
* `raw_location`: Original scraped string for debugging

### 3-Step Location Resolution Pipeline
1. **Alias & Neighborhood Mapping (`config/india_geo.yaml`):** Map alternate spellings, airport codes, and tech-hub neighborhoods to canonical cities:
   * `hyderabad`: `Hyderabad`, `HYD`, `HITEC City`, `Hi-Tech City`, `Gachibowli`, `Madhapur`, `Kondapur`, `Secunderabad`, `T-Hub`, `Telangana`
   * `bengaluru`: `Bengaluru`, `Bangalore`, `BLR`, `Koramangala`, `HSR Layout`, `Indiranagar`, `Whitefield`, `Bellandur`, `Electronic City`, `Karnataka`
   * `chennai`: `Chennai`, `Madras`, `MAA`, `OMR`, `Guindy`, `Tidel Park`, `Perungudi`, `IITM Research Park`, `Tamil Nadu`
   * `delhi_ncr`: `Delhi`, `New Delhi`, `Gurugram`, `Gurgaon`, `Noida`, `Greater Noida`, `Faridabad`, `NCR`
   * `mumbai`: `Mumbai`, `Bombay`, `Navi Mumbai`, `Thane`, `BKC`, `Powai`, `Andheri`
   * `pune`: `Pune`, `Hinjewadi`, `Baner`, `Kharadi`, `Magarpatta`
2. **Remote-India Detection:** Classify listings matching `Remote - India`, `Remote (IST)`, `Work from Home (India)`, `WFH`, or `Anywhere in India` as `work_mode = "remote_india"` so they appear when filtering by any Indian city with `--include-remote`.
3. **Startup HQ Fallback (PIN Code & Footer Inference):** If a job post omits a city (or says only `"India"`), infer the startup's HQ from:
   * Indian 6-digit PIN codes (`\b[1-8]\d{5}\b`) on `/contact` or footer addresses (e.g., `500xxx` -> Hyderabad, `560xxx` -> Bengaluru, `600xxx` -> Chennai).
   * Source directory metadata (e.g., any startup ingested from T-Hub defaults to `city_canonical = "hyderabad"`).

## 3. Scraping Methods by Category (India-Optimized)

### A. Explicit Internship Discovery (Public ATS Feeds)

Startups in India commonly use both global ATS platforms and SaaS-friendly job widgets that expose public JSON feeds:

1. **Greenhouse Job Board API** ([Greenhouse Job Board API Docs](https://docs.greenhouse.io/job-board.html))
   * **Endpoint:** `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
   * **Method:** Query seed Indian startup `board_token` slugs, filter for internship keywords (`\bintern\b`, `\binternship\b`, `\bsummer intern\b`, `\bwinter intern\b`, `\b6[- ]month\b`, `\bPPO\b`), and normalize `location.name` through `geo.py`.

2. **Lever Postings API** ([Lever Postings API Docs](https://github.com/lever/postings-api))
   * **Endpoint:** `GET https://api.lever.co/v0/postings/{company}?mode=json`
   * **Method:** Query unauthenticated v0 postings per company slug. Filter on `categories.commitment`, `categories.location`, and title/description keywords.

3. **Ashby Public Job Posting API** ([Ashby Public Job Posting API Docs](https://developers.ashbyhq.com/docs/public-job-posting-api))
   * **Endpoint:** `GET https://api.ashbyhq.com/posting-api/job-board/{jobs_page_name}?includeCompensation=true`
   * **Method:** Popular among AI and US-India cross-border startups (especially Bengaluru/Hyderabad engineering hubs).

4. **Workable Public Careers Widget** ([Workable Careers API Guide](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page) | [Workable Widget Reference](https://jobspipe.dev/sources/workable))
   * **Endpoint:** `GET https://apply.workable.com/api/v1/widget/accounts/{account}`
   * **Method:** Keyless JSON endpoint widely used by seed and Series A Indian SaaS startups; returns city, country, remote status, and job shortcode.

5. **Indian ATS Embed Detection (Tier 2/3)**
   * Auto-detect links/iframes to India-popular HR portals (`keka.com`, `zohorecruit.in`, `freshteam.com`, `darwinbox.in`) during careers-page crawls and route them to lightweight HTML parsers or the optional Tier 3 Firecrawl fallback.

### B. India Startup Directories, Incubators & Funding Feeds

Local incubators and Indian startup media are the highest-signal sources for city-specific early-stage startups:

1. **City-Anchored Incubator Directories (High-Signal Local Leads)**
   * **Hyderabad:** [T-Hub Startups Directory](https://www.t-hub.co/startups-directory) and [T-Hub Lab 32 Directory](https://programs.t-hub.co/lab-32-directory/) — structured listings of early-stage Hyderabad startups with sector and founder names.
   * **Chennai & Bengaluru:** Target public portfolio pages of regional deep-tech and SaaS incubators (such as IIT Madras Incubation Cell, NSRCEL at IIM Bangalore) plus India-filtered cohorts from accelerators like Y Combinator and Peak XV Surge.
2. **Indian Startup Funding Feeds (RSS & News)**
   * **Inc42 Startup Stories RSS** (`https://inc42.com/startups/feed/`, via [Inc42 Startup Ecosystem](https://inc42.com/industry/startup-ecosystem/)) — poll daily via `feedparser` for newly funded Pre-Seed and Seed Indian startups.
   * **Entrackr Startup Funding** ([Entrackr Funding Tag](https://entrackr.com/tags/startup-funding)) — track weekly Indian funding roundups to extract startup name, funding amount, city HQ, and founders.
3. **Hacker News "Who is Hiring?" Threads** ([HN Search API Docs](https://hn.algolia.com/api))
   * Filter monthly hiring comments for `India`, `Bengaluru`, `Bangalore`, `Hyderabad`, `Chennai`, `IST`, or `Global Remote` alongside internship/junior keywords.

### C. Hidden-Opportunity Discovery Methods

Most Indian early-stage startups hire interns via cold outreach rather than formal job boards:

1. **Careers Page & ATS Auto-Discovery Crawler**
   * For every newly discovered Indian startup domain, probe `/careers`, `/jobs`, `/join`, `/work-with-us`, and `/about` (or use Firecrawl `Map` when enabled).
   * Extract embedded ATS slugs (`greenhouse`, `lever`, `ashby`, `workable`, `keka`, `zohorecruit`) to auto-enroll the company in Tier 1 polling.
   * Flag pages containing "General Application", "Drop your resume", or direct founder emails as `open_to_cold_pitch = True`.
2. **Engineering Velocity via GitHub** ([GitHub REST API: Orgs](https://docs.github.com/rest/orgs) & [Repos](https://docs.github.com/en/rest/repos))
   * Query linked startup GitHub orgs for recent commit activity (last 14–30 days), new repos, `good first issue` tags, and primary tech stack.
3. **India Hidden-Opportunity Scoring Rubric (0–100 Points)**
   * **+30 pts:** Raised Pre-Seed or Seed funding in India within the last 60 days (from Inc42/Entrackr/accelerator announcements).
   * **+20 pts:** Located in a user-selected target city (e.g., Hyderabad, Bengaluru, Chennai) or explicitly supports `remote_india`.
   * **+20 pts:** Active GitHub org (3+ active repos or recent commits in the last 14 days).
   * **+15 pts:** Careers page invites cold pitches or lists a direct founder/recruiting email.
   * **+15 pts:** Small team size (2–35 people) or tech stack matches target skills.

### D. India-Specific Stipend & Role Metadata Extraction
Parse Indian internship conventions cleanly from job descriptions:
* **Stipend Regex:** Capture `₹15,000`, `INR 20,000/month`, `15k - 25k/mo`, and flag `unpaid` vs. `paid`.
* **Duration & Conversion:** Extract `2 months`, `3 months`, `6 months`, and `PPO` (Pre-Placement Offer) indicators.

### E. Contact Discovery & Verification Methods

1. **Pass 1 — Direct Public Website Extraction:** Crawl `/contact`, `/about`, `/team`, `/careers`, and footer HTML for `mailto:` links, hiring emails (`founders@`, `careers@`, `jobs@`, `hiring@`, `talent@`, `hello@`), and public founder social URLs.
2. **Pass 2 — Public GitHub Metadata:** Check the startup's GitHub org profile and public members via the [GitHub REST API](https://docs.github.com/rest/orgs).
3. **Pass 3 — Pattern Generation + DNS MX Verification:** Generate standard work patterns (`first@domain`, `first.last@domain`) from public founder names (e.g., from T-Hub or Inc42 articles) and verify domain mail capability via DNS **MX records** (`dnspython`). Never run aggressive SMTP `RCPT TO` probing.
4. **Confidence Tagging:** Label contacts as `direct_public` (`0.95`), `github_public` (`0.85`), or `pattern_mx_verified` (`0.50`).

## 4. Data Models, Deduplication & CLI Filtering

### Core Pydantic v2 Schemas
* **`Startup`**: `id`, `name`, `canonical_domain` (unique), `hq_city_canonical`, `hq_state`, `hq_lat`, `hq_lng`, `ats_provider`, `ats_slug`, `funding_stage`, `last_funded_at`, `github_org`, `hidden_score`, `open_to_cold_pitch`, `last_checked_at`.
* **`Opportunity`**: `id`, `startup_id`, `source`, `title`, `url`, `raw_location`, `city_canonical`, `state`, `work_mode`, `stipend_inr_min`, `stipend_inr_max`, `duration_months`, `has_ppo`, `is_hidden_lead`, `tech_stack`, `posted_at`, `status`.
* **`Contact`**: `id`, `startup_id`, `full_name`, `role`, `email`, `profile_url`, `source_method`, `confidence_score`, `verified_mx`, `discovered_at`.

### Deduplication Keys
* **Startups:** Upsert on `canonical_domain` (`https://www.acme.in/careers` -> `acme.in`).
* **Opportunities:** Upsert on deterministic hash of `(canonical_domain, normalized_title, source_url)`.
* **Contacts:** Upsert on `(startup_id, lowercase(email))` or `(startup_id, profile_url)`.

### Geolocation CLI Examples
```bash
# Export Hyderabad internships + India-remote roles
uv run scraper export --cities hyderabad --include-remote --format csv

# Export multi-city South India startup internships with high hidden-opportunity scores
uv run scraper export --cities hyderabad,bengaluru,chennai --work-mode onsite,hybrid --min-hidden-score 60

# Run a targeted discovery pass for Hyderabad startups
uv run scraper scrape --sources thub,greenhouse,ashby,workable --city hyderabad
```

## 5. Politeness, Reliability & Vibe-Coding Standards

1. **Robots.txt Compliance:** Follow each domain's `/robots.txt` per [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309) before crawling HTML pages.
2. **Rate Limiting & Caching:** Enforce `1.5–2.5s` per-domain jittered delays, max `5` concurrent workers, `ETag`/`If-Modified-Since` caching, and max 3 exponential-backoff retries on `429`/`5xx`.
3. **Fault Isolation:** Every collector runs inside an exception boundary so one broken site never stops other collectors.
4. **Offline Fixture Tests:** Save 1–2 anonymized sample JSON/HTML responses per source under `tests/fixtures/` and unit-test `parse()` plus `geo.py` city resolution offline with `pytest`.
5. **Config-Driven Rules:** Keep city aliases (`config/india_geo.yaml`), seed slugs (`config/sources.yaml`), and scoring weights (`config/scoring.yaml`) outside Python code so future devs can add cities or sources without touching scraper logic.

## 6. Build Sequence

1. **Phase 1 — Foundation & Geo Engine:** Initialize `uv`, `Ruff`, `AGENTS.md`, Pydantic models, SQLite storage, and `src/enrichment/geo.py` with `config/india_geo.yaml` unit tests.
2. **Phase 2 — Core HTTP & Compliance Layer:** Build shared `httpx` client with rate limiting, retries, `robots.txt` checker, and `BaseCollector`.
3. **Phase 3 — Tier 1 ATS Collectors:** Implement Greenhouse, Lever, Ashby, and Workable collectors with India city filtering and offline fixture tests.
4. **Phase 4 — India Hidden-Opportunity & Contact Pipeline:** Add T-Hub, Inc42/Entrackr RSS, HN Hiring, GitHub velocity checker, ATS auto-discovery, 3-pass contact enrichment, and optional Firecrawl Tier 3 fallback.
5. **Phase 5 — Geolocation CLI & Automation:** Wire `Typer` CLI (`--cities`, `--include-remote`, `--min-hidden-score`), CSV/Sheets export, GitHub Actions daily cron, and finalize `README.md` + `docs/ARCHITECTURE.md`.
