# ADR 003 — ATS Collectors, India Filtering and Internship Terms

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair (Phase 3)

## Context
Phase 3 adds the four Tier 1 collectors (Greenhouse, Lever, Ashby, Workable). Probing the live APIs
(sequential, ~2 s apart, project User-Agent) showed shapes that differ from the docs in ways that
matter for India: location strings like `"Onsite in Bangalore"`, double-escaped HTML, internships
labelled `FullTime` in Ashby, `salaryRange: {0, 0}` meaning "undisclosed" in Lever, and global
boards mixing Bengaluru roles with Kuala Lumpur, Cairo or San Francisco ones.

## Decisions

### 1. One shared pipeline, thin provider adapters
`AtsCollector` owns filtering, geo, India filtering and term extraction; each provider implements
only `postings(raw)` and `to_job(posting) -> AtsJob`. Adding a fifth ATS is ~50 lines plus a fixture.
Per-posting `try/except` means one malformed job never drops a whole board.

### 2. Internship = title or employment-type, never description
Descriptions of full-time roles routinely say "mentor interns". We match the title against
`include_patterns`, or the ATS employment-type/department field against `employment_type_patterns`,
and veto on seniority/contract words. **"Manager" and "Lead" are not exclusions** ("Product Manager
Intern", "Lead Generation Intern" are real). Word boundaries keep "Internal Audit" out.

### 3. Seed HQ is a fallback for *generic* locations only
The seed's `hq_city` is applied when a posting's location is empty, only "India", or a bare
"Remote" — never when the location names a place we cannot map, and never when the ATS gives a
non-Indian country code. A wrong HQ fallback silently mislabels foreign jobs as Indian, which is
worse than dropping them. `india_geo.yaml` gained ~25 foreign markers (Malaysia, Egypt, Paris, …)
found on the seeded boards.

### 4. Drop non-India postings in the collector
The product is India-first; Phase 5 filters by city. Collectors keep `country == IN` (cities and
`remote_india`) and drop the rest (`india_only=False` exists for debugging).

### 5. Conservative stipend parsing
Structured INR **monthly** compensation wins; otherwise the text parser accepts an amount only with
a monthly cue, the word "stipend", or a rupee marker, within ₹1k–₹5L/month. Annual figures (`LPA`,
`per annum`, `CTC` — usually the PPO package), foreign currencies (explicit code list) and durations
are rejected. Durations ignore "N months of experience" / "notice period"; ranges keep the upper bound.
False negatives are acceptable; false stipends would mislead students.

### 6. Collectors emit `CollectedOpportunity`; `src/pipeline.py` persists
Collectors cannot know DB ids, so they emit `(startup, opportunity)` pairs. `persist_results()`
upserts the startup once per domain (stamping `last_checked_at`), then the opportunity with that id.
It is the only module that combines collectors and storage.

### 7. Seeds are verified, never guessed
All 15 seeds were confirmed on the live public API on 2026-11-26 (`verified_on`); domains were
checked to resolve. `hq_city` is omitted where the HQ city is uncertain (Plane, Atlan). Workable
seeds are real accounts that had zero open jobs that day; unknown Workable accounts return 404.

## Consequences
- A daily run over the 15 seeds takes ~10 s (one request per board, polite pacing) and found
  6 India internships on 2026-11-26.
- Postings that disappear from a board are not yet marked `closed` — Phase 5 should close
  opportunities whose `last_seen_at` predates the latest successful run of their board.
- Tech-stack extraction from descriptions is not done yet (`tech_stack` stays empty).
