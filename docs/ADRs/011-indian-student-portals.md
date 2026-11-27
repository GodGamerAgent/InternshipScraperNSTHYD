# ADR 011 — Indian Student Portals (Batch C)

- **Status:** Accepted (awaiting user review)
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair

## Context
Internshala, Unstop, Cuvette, Cutshort and Instahyre hold most Indian student internships. Batch C
required a compliance audit before any code. For each site the audit checked robots.txt,
unauthenticated access and bot protection, plus the site's Terms of Service on automated access.

## Audit (2026-11-26)
Our User-Agent was used, robots.txt first, with at most one listing page plus the Terms page per site.

| Portal | robots.txt | Unauthenticated access | Bot protection | Terms of Service |
|---|---|---|---|---|
| Internshala | `/internships/…` and `/page-N/` allowed. Disallowed: `/internship/details/`, `/internship/search/`, `/api/`, any `?query` | 200; 50 server-rendered cards per page | None seen (reCAPTCHA only on login/apply forms) | Prohibits automated access, scraping and crawling |
| Unstop | `/internship/` and `/api/public/*` allowed | **403 Forbidden** (CloudFront) for our UA | **Firewall block** | not checked |
| Cuvette | — | **`cuvette.tech` does not resolve** (NS records, no A record) | — | — |
| Cutshort | Listing pages allowed; `/view/j/`, `/vj/`, `/apply/`, `/*?job_listing` disallowed | 200; jobs in server-side `__NEXT_DATA__` | Cloudflare, no challenge served | Prohibits automated access and scraping |
| Instahyre | `User-agent: *` with no rules | 200; landing page embeds JSON, and `/api/v1/job_search` answers without login | Cloudflare, no challenge served | Prohibits crawling and spidering |

## Decisions
1. **The owner chose to collect from every portal with unauthenticated public access, despite
   its Terms of Service** ("If public access is there go for it"). The audit recommended no-go
   for all five; this ADR records the decision and the risk. **Risk:** collecting against a
   site's Terms can lead to IP blocking or a legal complaint. The data is limited to public
   internship listings for personal, human-reviewed use; no accounts are used, nothing is
   republished, and no seeker data is touched. Revisit this decision if any portal objects or
   offers an official feed.
2. **Built:** `internshala`, `cutshort`, `instahyre`. The guardrails that still apply, and are
   enforced in code or config:
   - robots.txt is respected on every request, using the default `HttpClient` path.
   - Detail and apply pages are never fetched, only listing pages and the site's own API links.
   - Each request goes through the standard per-host pacing (1.5–2.5 s jitter, ≤ 5 concurrent,
     ≤ 3 retries), and the number of pages is bounded.
   - No login, cookies, CAPTCHA solving, firewall or Cloudflare bypass. A 403, challenge or
     robots change fails the target as an issue, and nothing retries it another way.
   - Config validation rejects query strings, `#` fragments and paths outside `/internships/`
     or `/jobs/`. Instahyre follows only its own relative `/api/v1/job_search` `next` links.
   - Personal data is never read. Cutshort's `createdBy` (recruiter) is skipped, and a test
     asserts the recruiter name is absent from the output.
3. **Non-targets. Do not re-attempt either one:**
   - **Unstop:** its CloudFront firewall returns 403 to a transparent crawler. Getting past it
     would mean bypassing bot protection.
   - **Cuvette:** the domain does not resolve. Re-audit only if a new official domain is
     confirmed.
4. **Shapes and mapping:**
   - **Internshala:** `div.individual_internship[internshipid]` cards: title, company, locations,
     `span.stipend`, duration, summary. Only `employment_type="internship"` cards are kept. The
     targets are city pages for bengaluru, hyderabad and chennai plus work-from-home, ×
     `max_pages` (2).
   - **Cutshort:** `__NEXT_DATA__ … pageData.jobs[]`, kept when `roleTypes` contains
     `internship`. Location comes from `locationsText` + `remoteType`, stipend from
     `salaryRangeText`. The `Startup` comes from the company website Cutshort publishes
     (`companyDetails.links.website`; social or registry hosts are rejected). Page 1 of each
     sitemap-listed city page plus `remote-jobs`.
   - **Instahyre:** `<script id="initial-job-search">`, where `params.job_type == "2"` means
     internships. It follows `meta.next` for up to `max_pages` (5) responses.
   - **Location:** `portal_base.india_portal_location`. These portals are India-only, so generic
     remote text or an unknown town resolves to India (`remote_india` / India-unknown). A place
     `GeoResolver` marks as foreign is dropped.
   - **Stipend, duration, PPO:** `stipend.py` applied to the listing text.
   - **City scoping:** `--cities` narrows the Internshala and Cutshort city targets; `remote` is
     always kept.
   - **Closing:** each collector is one target with `close_scope = "*"`, so closing happens only
     after a fully successful read.

## Consequences
- These collectors are part of the default `scraper scrape`, and so of the daily workflow. To
  stop collecting from a portal, remove its block from `sources.yaml: student_portals` (the
  collector then has no targets).
- Live check on probe pages (2026-11-26):
  - Internshala: 154 internships from 3 pages; 127 with a stipend, 152 with a duration.
  - Cutshort: 10 remote internships, all with company domains.
  - Instahyre: 18 of the first 20.
- The parsers depend on page markup. A redesign shows up as a `parse` issue in the run report.
