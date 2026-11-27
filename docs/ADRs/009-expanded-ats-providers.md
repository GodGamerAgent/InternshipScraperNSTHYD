# ADR 009 — Expanded Public ATS Providers (Batch A)

- **Status:** Accepted (awaiting user review)
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair
- **Note:** The batch brief asked for `008-expanded-ats-providers.md`, but 008 already covers v0.2
  (Indian ATS portals & directories), so this ADR is 009.

## Context
Batch A adds the public job-board APIs of SmartRecruiters, Rippling, BreezyHR and Recruitee. The
batch's compliance gate:
- never bypass CAPTCHAs, login walls or robots.txt;
- skip a target cleanly and record why;
- seeds must be live-verified, never guessed.

## Probe results (2026-11-26, our own User-Agent, robots.txt checked first)
| Provider | Endpoint | robots.txt | Decision |
|---|---|---|---|
| **SmartRecruiters** | `api.smartrecruiters.com/v1/companies/{id}/postings` | `User-agent: LinkedInBot` → `Allow: /v1/companies/`; `User-agent: *` → **`Disallow: /`** | **Skipped (non-target).** It is detection only: `ats_discovery` records `jobs.smartrecruiters.com/{id}` links, but nothing polls them. Do not re-attempt through the API or by scraping `jobs.smartrecruiters.com` HTML |
| **Rippling** | `api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` | `api.rippling.com`: 404, so allowed (RFC 9309). `ats.rippling.com`: only `/internal/` disallowed | `rippling` collector |
| **BreezyHR** | `https://{slug}.breezy.hr/json` | Per-tenant robots.txt allows it. `breezy.hr` itself only disallows `/api/`, `/app/` and similar paths | `breezyhr` collector |
| **Recruitee** | `https://{slug}.recruitee.com/api/offers/` (Careers Site API) | Per-tenant robots.txt allows it | `recruitee` collector |

Our existing Tier 1 paths were re-checked under the new gate and are **allowed** by each host's
robots.txt:
- `boards-api.greenhouse.io/v1/boards/…`
- `api.lever.co/v0/postings/…`
- `apply.workable.com/api/v1/widget/…`
- `api.ashbyhq.com` (its `/robots.txt` returns 401, which counts as no restrictions)

So ADR 002's opt-out does not conflict with the gate today.

## Decisions
1. **Three new `AtsCollector` subclasses** (`rippling.py`, `breezyhr.py`, `recruitee.py`) reuse:
   - `InternshipMatcher`;
   - `GeoResolver` with the generic-only HQ fallback;
   - the India filter;
   - `stipend.py` (Recruitee's structured INR monthly salary; Breezy's free-text `salary`).

   `close_scope` is the seed's domain.
2. **robots.txt stays on for every new provider** (`AtsCollector.respect_robots = True`).
   - `ats_probe.fetch_board` likewise opts out of robots only for the four ADR 002 APIs
     (`ROBOTS_OPT_OUT`).
   - A provider changing its robots.txt therefore stops collection cleanly, as a
     `RobotsDisallowedError` issue. There is no workaround.
3. **Shapes, verified 2026-11-26:**
   - **Rippling:** a list of `{uuid, name, department, url, workLocation.label}`. There is no
     employment type, so the internship filter matches on the title. A `Remote…` / `Hybrid…`
     label prefix sets the workplace.
   - **Breezy:** a list of `{name, url, type{id,name}, location{city, country{id,name},
     is_remote}, locations[], salary, published_date, company{name}}`.
   - **Recruitee:** `{"offers": [{title, careers_url, status, employment_type_code, locations[],
     country_code, remote, hybrid, on_site, salary{min,max,period,currency}, published_at
     "YYYY-MM-DD HH:MM:SS UTC", description, requirements}]}`. Only `published` offers are kept.
4. **Config and wiring:**
   - `AtsProvider` gains `rippling`, `breezyhr`, `recruitee` and `smartrecruiters`.
   - `JSON_ATS_PROVIDERS` (Tier 1 plus the three new providers) is what `ats_probe.providers` and
     `ats_search.queries` may use.
   - `POLLED_ATS_PROVIDERS` adds Zoho on top, for polling and `discovered_seeds`.
   - A `sources.yaml: ats` section without a collector (e.g. smartrecruiters) is a validation
     error.
   - `ats_embed_hosts`, `ats_discovery` URL patterns, `BOARD_URLS`, board-name extraction
     (Breezy `company.name`, Recruitee `company_name`) and `scraper discover --provider` all
     include the new providers.
5. **Seeds: 11, each verified live.** A seed needs Indian postings **and** evidence for its domain.
   - **Domain evidence:** for Breezy and Recruitee, the tenant portal links the company domain.
     For Rippling, the company site links the exact board (azira.com/careers →
     `ats.rippling.com/azira-careers-page`) or the job JSON links it (rippling.com).
   - **Rippling (2):** Rippling and Azira. TYLsemi, OneOrigin and Genios AI have Indian roles but
     no verifiable domain: their boards are JS-only, and their sites show no back-link or block
     us. They were left out rather than guessed.
   - **BreezyHR (5):** 75F, Anovia, sa.global, NationsBenefits and SkySpecs.
   - **Recruitee (4):** Lenskart, TransPerfect, MGID and Framestore. Gain was left out because
     its portal links both gain.pro and gain.ai.
   - Most are India offices of global companies, not Indian startups. On 2026-11-26 their
     boards held few internships (Lenskart 2, TransPerfect 2). `enrich` and `discover` add more
     boards over time.

## Consequences
- Every new provider costs one extra robots.txt request per host per run (cached).
- Rippling listings carry no employment type, so an internship titled without "intern" or
  "trainee" is missed.
- SmartRecruiters coverage is detection only unless SmartRecruiters changes its robots.txt.
