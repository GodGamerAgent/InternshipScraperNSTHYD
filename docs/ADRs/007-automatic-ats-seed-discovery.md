# ADR 007 — Automatic ATS Seed Discovery

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair
- **Note:** v0.2 (Indian ATS portals & regional directories) was paused for this work and will use ADR 008.

## Context
Tier 1 coverage depends on knowing each startup's public ATS board. Hand-verified seeds in
`config/sources.yaml` do not scale, and careers-page embed detection (ADR 004) misses startups whose
careers page links nowhere. We want the scraper to find and verify boards by itself without
editing files under version control, without scraping search-engine HTML, and without attributing
another company's board to a startup (slug collisions are common: `atlan`, `plane`, `zeta`).

## Decisions

### 1. Discovered boards live in SQLite, never in `sources.yaml`
Automated runs only *read* `sources.yaml`. A verified board is stored on the startup row
(`ats_provider`, `ats_slug`, `discovered_via`), and `discovered_seeds(repo.list_startups())` already
merges those into every `scraper scrape` (YAML seeds win on duplicate slugs). Git stays clean; a test
asserts `sources.yaml` is byte-identical after a discovery run.

### 2. Slug probing during `enrich` (`src/enrichment/ats_probe.py`)
Runs only when a startup has a domain, no `ats_provider`, and its careers pages embedded no board.
Candidates come from the registrable domain label (`getacme.co.in` → `getacme`, `acme`) and the
company name, skipping anything shorter than `min_slug_length` (4) or in `stopwords`, capped at
`max_candidates` (3). Providers are tried in order on their documented public JSON endpoints
(`respect_robots=False`, ADR 002; pacing/retries/ETag cache still apply); the first board that
passes the anti-collision check wins. Worst case: 3 × 4 = 12 small requests per startup.

### 3. Anti-collision: a 200 OK is not proof
A board is accepted only if (a) a link inside its postings points at the startup's registrable
domain, or (b) its display name (Greenhouse `company_name`, Workable `name`) **exactly** equals the
startup's name or domain label after removing legal suffixes and configured `name_noise` words
("careers", "software", "pvt", …). Prefix matching was tried first and rejected: it accepted
"Atlan Travel Group" for "Atlan" (caught by tests). Empty boards with no name are rejected.
Live check (2026-11-26): Groww's board accepted by name; Atlan's Ashby board accepted for `atlan.com`
and rejected for an unrelated `atlan.in`.

### 4. Search-based discovery (`scraper discover`, `src/collectors/ats_search.py`)
- **Official APIs only:** Brave Search API (`BRAVE_SEARCH_API_KEY`, preferred) or Firecrawl `/search`
  (`FIRECRAWL_API_KEY`). No key → setup hint, exit 0, zero network calls. Keys only in headers.
- Query templates (`site:jobs.lever.co intern {city}`, …) live in `sources.yaml: ats_search`; cities
  are canonical slugs mapped to display names, plus an "India" query.
- `(provider, slug)` pairs are parsed from result URLs (`find_ats`), boards already known (YAML or DB)
  are skipped, each new candidate is verified on the live ATS API (404 → `not_found`), and the
  company domain is **inferred** from the board's own links (most-linked non-ATS/non-social domain
  whose brand label matches the slug or board name).
- **No invented domains:** a verified board without a plausible domain is stored as a `StartupLead`
  (`source="ats_search"`, `source_url` = board URL) for human review — it is not polled.
- **No overwrites:** if the inferred domain already has a *different* board, it is a `conflict`.

### 5. Surfaces
`scraper discover [--provider all|greenhouse|lever|ashby|workable] [--cities …] [--limit N]
[--dry-run]`; NiceGUI **Discover boards** button (shares `JOB_LOCK`, shows a key hint).

## Consequences
- Coverage grows automatically: enrich probes known startups; discover finds new ones.
- The Brave and Firecrawl `/search` parsers follow the documented response shapes and are tested with
  mocks only — verify with a real key before relying on them.
- Exact-name matching trades recall for precision: some real boards (no company link, non-matching
  display name) are missed rather than mis-attributed.
