# Project Context: India Startup Internship & Hidden-Opportunity Scraper

## 1. Project Vision & Problem Statement

Finding high-quality startup internships in India is broken for two reasons:
1. **Crowded aggregators:** Mainstream job portals are flooded with thousands of applicants per listing, outdated posts, and low-signal roles.
2. **Hidden demand:** Most early-stage Indian startups (Pre-Seed to Series A) hire interns through founder networks, community threads, or cold outreach—never posting on public job boards.

This project solves both problems by building an automated, modular pipeline that:
* Scrapes fresh, public internship openings directly from startup ATS feeds and developer communities.
* Surfaces **hidden opportunities** by tracking recently funded Indian startups, local incubator cohorts, and active GitHub engineering organizations.
* Discovers and verifies **public recruiting/founder contacts** ethically so users can send targeted outreach.
* Filters every result cleanly by **Indian geolocation** (e.g., Hyderabad, Bengaluru, Chennai, Mumbai, Delhi NCR, Pune) and **Remote India** eligibility.

## 2. Target Users & Core Workflows

* **Primary User:** Students and early-career builders in India targeting engineering, AI/ML, product, or growth internships at high-growth startups.
* **Daily Workflow:**
  1. Run a scheduled or manual scrape targeting Indian startup sources.
  2. Enrich discovered startups with city/remote geolocation, hidden-opportunity scores, and public contacts.
  3. Export a clean, deduplicated CSV or JSON list filtered by target city (e.g., `--cities hyderabad,bengaluru,chennai --include-remote`) for direct applications and cold outreach.

## 3. Product Boundaries (In-Scope vs. Out-of-Scope)

### In-Scope (v1)
* API-first scraping of public ATS job boards (Greenhouse, Lever, Ashby, Workable).
* India startup discovery via funding RSS feeds (Inc42, Entrackr), local incubator directories (e.g., T-Hub Hyderabad), and Hacker News hiring threads.
* Optional Tier 3 fallback via [Firecrawl](https://www.firecrawl.dev/) (`FIRECRAWL_API_KEY`) for JS-heavy Indian startup websites.
* Rule-based hidden-opportunity scoring (0–100) using funding recency, GitHub velocity, careers-page cold-pitch signals, and city match.
* 3-pass public contact discovery (website extraction, GitHub public metadata, DNS MX-verified work email patterns).
* Deterministic India city/neighborhood/PIN-code geolocation resolver.
* Local SQLite storage with idempotent deduplication, `Typer` CLI, CSV/JSON exports, and GitHub Actions daily scheduling.

### Out-of-Scope (Do Not Build in v1)
* Automated mass cold-email sending or LinkedIn DM bots (outreach remains human-reviewed).
* Scraping login-walled social platforms (LinkedIn, private X/Twitter) or bypassing CAPTCHAs.
* Heavy web frontend, user authentication, or paid SaaS multi-tenancy (keep v1 CLI-first and zero-infrastructure).
* Aggressive SMTP `RCPT TO` mail-server probing.

## 4. India Startup Ecosystem Nuances

Any contributor (AI or human) working on parsers or enrichment must account for these local realities:
* **Messy Location Strings:** Listings frequently use neighborhoods, old spellings, or airport codes instead of city names (`HITEC City` / `Gachibowli` = Hyderabad; `Bangalore` / `Koramangala` / `HSR` = Bengaluru; `OMR` / `Guindy` = Chennai; `Gurgaon` / `Noida` = Delhi NCR). Always normalize via `config/india_geo.yaml`.
* **Remote-India vs. Global Remote:** Many US/EU "Remote" roles restrict hiring to US time zones. Explicitly distinguish `remote_india` (IST-compatible or India-based WFH) from `remote_global`.
* **Compensation & Internship Signals:** Indian internships commonly list monthly stipends in INR (`₹15,000/mo`, `20k–30k`), durations (`2 months`, `6 months`), and **PPO** (Pre-Placement Offer) availability.
* **Regional Hubs:** Incubators like T-Hub (Hyderabad), IIT Madras Incubation Cell (Chennai), and NSRCEL (Bengaluru) are prime sources for early-stage startups that welcome direct student outreach.

## 5. Development Philosophy (Vibe-Coded, Dev-Grade)

This repository is built primarily through AI-assisted ("vibe-coded") sessions, but it is designed to be handed off to real software engineers without a rewrite:
* **Small, isolated modules:** Every scraper lives in its own file under `src/collectors/` behind `BaseCollector`.
* **Strict data contracts:** Every record passes through `Pydantic v2` models before hitting SQLite.
* **Config over hardcoding:** City aliases, seed startup lists, and scoring weights live in YAML files under `config/`.
* **Offline-first testing:** Every parser ships with anonymized HTML/JSON fixtures under `tests/fixtures/` so `pytest` runs fast with zero network calls.
* **Living documentation:** Keep `README.md`, `docs/ARCHITECTURE.md`, and `docs/ADRs/` updated whenever architecture or CLI flags change.

## 6. How Project Docs Fit Together

| File | Purpose |
| :--- | :--- |
| `Context.md` | The **why** — product vision, India domain context, scope boundaries, and current status. |
| `Implementation.md` | The **how** — exact scraping methods, API endpoints, geolocation logic, schemas, and guardrails. |
| `Prompt.md` | The **execution playbook** — phased prompts for building the repo step by step with Claude. |
| `AGENTS.md` | The **coding rules** — style, testing, and architectural constraints enforced on every AI session. |

## 7. Current Project Status

* **Stage:** Planning complete; ready to execute **Phase 1 (Foundation & Geo Engine)**.
* **Completed:** Architecture design, India-optimized scraping spec (`Implementation.md`), phased build prompts (`Prompt.md`), and project context (`Context.md`).
* **Next Milestone:** Scaffold the `uv` repository, `AGENTS.md`, `config/india_geo.yaml`, Pydantic models, SQLite deduplication layer, and `geo.py` unit tests.
