# ADR 010 — Remote Job Feeds Open to India (Batch B)

- **Status:** Accepted (awaiting user review)
- **Date:** 2026-11-26
- **Deciders:** project owner + AI pair
- **Scope note:** Batch B also listed YC India, IIT Madras and NSRCEL. Those already shipped in
  v0.2 (ADR 008), so this batch adds only the remote-job feeds.

## Context
Remote internships that a student in India can take are a large, hidden pool. Two public
remote-job boards were probed with our own User-Agent, robots.txt first (RFC 9309).

## Probe results (2026-11-26)
| Source | robots.txt | Endpoint used | Notes |
|---|---|---|---|
| **Himalayas** | `Allow: /`; disallows `/apply` and every `?page=` / `&page=` under `/jobs*` | `GET /jobs/api/search?country=IN&employment_type=Intern&sort=recent` and `?worldwide=true&employment_type=Intern&sort=recent` | Documented keyless JSON API (himalayas.app/api). 166 India interns, 20 per page; worldwide gave 3. **Page 1 only**, because later pages need the robots-disallowed `page=` |
| **Remotive (JSON API)** | **`Disallow: /api/*`** | — | **Non-target.** Do not use `/api/remote-jobs`, `/v2/feed` or `/remote-jobs/digest/feed` (all disallowed) |
| **Remotive (RSS)** | `/remote-jobs/feed` allowed | `GET /remote-jobs/feed` | 17 latest items, with free-text `location` and a `type`. 0 internships on probe day. `/remote-jobs/feed/{category}` → 404 |

Two Himalayas endpoints were rejected:
- The unfiltered `/jobs/api` browse feed (cursor-paged, allowed, 116k jobs) is far too large to
  filter locally for a few India internships.
- Search pages beyond 1 are robots-disallowed.

## Decisions
1. **Two collectors, `himalayas` and `remotive`**, emit `CollectedOpportunity` with
   `startup=None`, because neither feed exposes a verifiable company domain. The company name is
   prefixed to the description for human review. The internship test is the shared
   `InternshipMatcher` (title + `employmentType` / `type`); stipend, duration and PPO come from
   `stipend.py`. Himalayas' structured salary becomes a stipend only for INR monthly figures.
2. **"Open to India" is one shared rule** (`src/collectors/remote_base.IndiaOpenness`),
   configured in `sources.yaml: remote_feeds`:
   - **India-restricted**, or **IST-only** (timezones == {5.5}) → `remote_india`.
   - **Worldwide** → `remote_global`, kept while `keep_worldwide: true`. On Himalayas this means
     no country restriction and no timezone list that excludes IST. On Remotive it means the
     location text contains a `worldwide_keywords` term.
   - **Everything else** is dropped, e.g. US-only, "Europe", "LATAM", or a timezone list without
     IST. "APAC" / "Asia" are **not** treated as India-open by default; add them to
     `worldwide_keywords` to widen the rule.
   - `GeoResolver` produces the final `ResolvedLocation`, so these land in the same buckets as
     ATS and HN roles.
3. **Compliance is enforced in config validation**, not just in comments:
   - Himalayas queries may not contain `page`, `offset` or `cursor`.
   - The Remotive `feed_url` must be under `https://remotive.com/remote-jobs/feed`, so it can't be
     pointed at `/api/`.
   - Both collectors fetch with robots.txt respected, so a robots change stops collection with a
     `RobotsDisallowedError` issue.
4. **API terms:** Himalayas asks for `applicationLink` to be used unchanged and for Himalayas to
   be credited. The opportunity `url` is that link, and the source column says `himalayas`.
   Remotive items link to their remotive.com pages.
5. **Closing:** each collector has one target, with `close_scope = "*"`. Postings that vanished
   from the feed are closed only after a run in which every request succeeded.

## Consequences
- Each run makes 3 requests to Himalayas (robots.txt + 2 searches) and 2 to Remotive (robots.txt
  + RSS).
- Coverage is the 20 most recent India internships plus the most recent worldwide ones per run.
  Older matches are missed, and a still-open posting that falls off page 1 is marked closed. The
  daily schedule keeps this window current.
- The Remotive RSS rarely carries internships, so most of this batch's value comes from Himalayas.
