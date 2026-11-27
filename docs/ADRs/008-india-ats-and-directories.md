# ADR 008 — Indian ATS Portals & Regional Startup Directories (v0.2)

- **Status:** Accepted (awaiting user review)
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair

## Context
Many Indian startups hire through Keka or Zoho Recruit instead of the Tier 1 ATS JSON APIs, and
many are listed only in regional directories (YC's India list, IIT Madras and IIM Bangalore
incubators). v0.2 adds these sources. Each one was probed politely first: robots.txt checked
with our own User-Agent, then one or two sample requests, with raw samples kept outside the repo.

## Probe results (2026-11-26)
| Source | robots.txt | Data | Decision |
|---|---|---|---|
| **Keka** (`{co}.keka.com/careers`) | `Disallow: /`, `Allow: /careers` | `/careers` is a JS shell; jobs load from a disallowed path (`/ats/...`); no documented public API | **Not built.** Detection only (`ats_discovery` still records Keka slugs). Needs an owner decision |
| **Zoho Recruit** (`{host}/jobs/Careers`) | Only authenticated paths disallowed | Server-rendered; `<input id="jobs" value="[…JSON…]">` | `zohorecruit` collector |
| **YC India** | `api.ycombinator.com`: `Disallow: /` · `www.ycombinator.com/companies/location/india[/hiring]`: allowed | `data-page` JSON with `props.companies[]`, plus `hasMore` / `totalPages` | `yc_india` collector (the API and third-party mirrors are not used) |
| **IIT Madras Incubation Cell** | `/robots.txt` returns the SPA shell, so no rules | `GET /api/portfolios/active?page&limit` (425 rows; `limit=100` honoured) | `iitm` collector (robots respected) |
| **NSRCEL (IIM Bangalore)** | Allowed (only one plugin JSON is disallowed) | `/portfolio/`: 23 Elementor `*-startup-popup` blocks with "Key: Value" `<li>` rows | `nsrcel` collector |

## Decisions
1. **Zoho Recruit is a polled ATS** (`ZohoRecruitCollector(AtsCollector)`). It reuses the
   internship filter, `GeoResolver` with the generic-only HQ fallback, the India filter and
   `stipend.py`, and `close_scope` is the seed's domain.
   - Because it is an HTML page, `fetch` uses `get_text` with robots respected.
   - The seed `slug` is the **full portal host** (`acme.zohorecruit.in` or `.com`), because the
     data-centre TLD can't be guessed.
   - The config validator and `fetch` both reject anything that isn't `*.zohorecruit.(in|com)`,
     so a seed can never point the collector at an arbitrary host.
   - `ats_discovery` now captures the full host. `discovered_seeds` auto-enrolls Zoho boards
     and skips pre-v0.2 rows that stored only the subdomain.
   - `POLLED_ATS_PROVIDERS` (Tier 1 plus Zoho) controls polling. `ats_probe` and `ats_search`
     stay Tier 1 only, because they verify boards through JSON APIs.
   - 5 seeds, each verified live: it has Indian postings **and** the portal itself links the
     company's own domain. `hq_city` is omitted because the postings carry their own city.
2. **Keka is skipped cleanly.** Its data path is robots-disallowed, and AGENTS §4 says never
   work around robots. The options are left to the owner: ask each company for a public feed,
   or render `/careers` through Firecrawl (the rendered page would still call the disallowed
   path). **Not recommended.**
3. **Directories emit `StartupLead`s**, so the pipeline promotes leads that have a domain to
   `Startup`, and `enrich` then discovers their ATS boards and public contacts.
   - `yc_india` reads the listing and hiring pages with pagination capped by `max_pages`. It
     keeps `ycdc_status ∈ statuses` and `country == IN`.
   - A company counts as hiring if it appears on the `/hiring` listing. YC has no per-company
     hiring flag.
   - `iitm` (`kind: iitm_api`) and `nsrcel` (`kind: nsrcel_popup`) are new `incubators` kinds.
     Each collector takes only its own kinds, and `thub` keeps `tablepress` / `webflow`.
   - `default_city` (chennai / bengaluru) applies **only** when a row has no city, or just
     "India", through `GeoResolver`'s source-default rule.
   - NSRCEL falls back to the optional Firecrawl renderer, or `JavaScriptRequiredError`, if
     the popups ever move behind JavaScript.
4. **Schema change (additive migration).** `StartupLead` and the `leads` table gain nullable
   `batch`, `is_hiring`, `team_size` and `github_org` columns.
   - `team_size` and `github_org` flow into `Startup` via `to_startup()`.
   - `Repository.create_schema()` now runs `ALTER TABLE … ADD COLUMN` for any **nullable**
     column missing from an older database file.
   - It refuses, and raises, for non-null, primary-key or unique columns. Nothing is renamed or
     dropped; existing dedup keys are unchanged.
   - The `leads` export gains `batch`, `is_hiring` and `team_size`.
5. **Surfaces:** `zohorecruit`, `yc_india`, `iitm` and `nsrcel` are in `ALL_COLLECTOR_NAMES`,
   so they work with `scraper scrape --sources …`, the default run and the NiceGUI selector.
   The default run still excludes the `inc42` / `entrackr` aliases (`DEFAULT_COLLECTOR_NAMES`).

## Consequences
- Request cost per run:
  - YC: ~6 pages.
  - IITM: 5 pages of 100.
  - NSRCEL: 1 page.
  - Zoho: 1 page per portal.
  - All go through the same per-host pacing and robots cache.
- These parsers depend on page markup (Zoho `input#jobs`, YC `data-page`, NSRCEL popup classes).
  A redesign shows up as a `parse` issue in the run report, not as silent data loss.
- Keka coverage is limited to detection until the owner decides otherwise.
