# ADR 004 — Leads, Discovery Sources, Scoring and Contact Guardrails

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair (Phase 4)

## Context
Phase 4 adds hidden-opportunity sources (funding news, incubator directories, HN), careers-page
discovery, GitHub signals, a 0–100 score and public contact discovery. Polite probes on
2026-11-26 established:
- Funding news (Inc42, Entrackr) and many Lab 32 rows have **no company website**, yet
  `Startup.canonical_domain` is required and unique.
- `www.t-hub.co/startups-directory` renders its list with JavaScript; Lab 32 is server-rendered.
- Entrackr's `/feed` is 404 but `/rss` works; Inc42's funding headlines are on `/buzz/feed/`
  (`/startups/feed/` carries stories).
- HN "Who is hiring?" threads also contain **job-seeker** posts with personal details.

## Decisions

### 1. `StartupLead` + `leads` table (new natural key: normalized name)
A lead is a company sighting that may lack a domain. Key = `lead_name_key(name)` (normalized,
trailing legal suffixes removed; brand words kept). When a lead has a domain, the pipeline also
upserts a `Startup` and links `leads.startup_id`. Leads without a domain are kept for review and
for founder names, but cannot be enriched. Cross-source merging by name is approximate by design.

### 2. Directory and news parsing rules
- Incubator `default_city` applies **only when the HQ cell is empty**; "Colombo" never becomes
  Hyderabad (same lesson as ADR 003's HQ fallback).
- Website cells pointing at registries/social/job boards (`non_company_hosts`, e.g. zaubacorp)
  are not a company domain.
- Funding stage = the stage mentioned **first** in title + summary (later mentions are history);
  "pre-Series A" is bucketed with seed. Roundups and multi-company headlines are rejected.

### 3. JS-only pages: report, or render via optional Firecrawl
No headless browser. A JS-only page fails with `JavaScriptRequiredError` unless
`FIRECRAWL_API_KEY` is set; then `FirecrawlClient` checks the target's robots.txt with our UA
before delegating. Webflow card selectors for the rendered T-Hub page are assumptions until verified
with a real key.

### 4. Cold pitch only on hiring pages
"Write to us" in a homepage footer is not an invitation to pitch. Phrases count only on
careers/jobs/join/work-with pages (or their redirect targets). Direct recruiting emails also satisfy
the rubric's cold-pitch signal, via contacts.

### 5. Contacts: public, company-domain, MX-only
- Pass 1 keeps emails on the company's own domain (minus press@/sales@/…) and person profile
  links (LinkedIn `/in/`, GitHub user, X) — never fetched.
- Pass 2 keeps the org email and members' profiles; members' emails only if on the company domain.
- Pass 3 guesses `first@` / `first.last@` from public founder names **only after a DNS MX lookup**
  (null MX = no). `verified_mx=True` means "domain accepts mail", not "mailbox exists".
- No SMTP code exists; a test fails if `smtplib`/`aiosmtplib` is ever imported under `src/`.
- Only the GitHub org used for signals is excluded from person profiles (other `github.com/<x>`
  links on team pages are usually people — a bug found by the end-to-end test).

### 6. Scoring is all-or-nothing per signal and explainable
`ScoringConfig` validates `scoring.yaml` (points must sum to `max_score`). Each signal awards full
points or none and adds a reason string, so every score can be explained in exports.

### 7. Auto-enrollment of discovered ATS boards
A Tier 1 board found on a careers page sets `Startup.ats_provider/ats_slug`;
`discovered_seeds()` turns those into extra seeds for `build_ats_collectors(extra_seeds=…)`
(YAML seeds win on duplicate slugs).

## Consequences
- Live run (2026-11-26, discovery collectors only): 21 funding leads from 3 feeds, 287 Lab 32 leads
  (149 with a real website → startups), 1 HN posting, T-Hub main directory reported as JS-only.
- Enrichment was verified offline only; it was deliberately not run against real companies, to avoid
  collecting real people's contact data during development.
- Phase 5 should expose leads in exports and add a way to attach a domain to domain-less leads.
