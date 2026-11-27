# Architecture

## 1. Pipeline at a glance

```
                ┌──────────────── config/*.yaml (geo, sources, scoring) ────────────────┐
                ▼                                                                         ▼
 sources ──► collectors ──► models (Pydantic) ──► enrichment ──────────────► storage (SQLite) ──► cli export
 (ATS JSON,   (BaseCollector   Startup /           geo → stipend → scorer      Repository: idempotent   CSV / JSON
  RSS, HTML,   subclasses,     Opportunity /       → contacts → ats_discovery  upserts + dedup keys     filtered by
  HN, GitHub)  one per file)   Contact             (pure transforms)                                    city/mode/score
                    │
                    └── src/core/http.py (rate limit, retries, ETag cache) + robots.py (RFC 9309)
```

**Design rules** (enforced by `AGENTS.md`):
- Collectors only *fetch and parse*. They return models; the orchestrator persists them.
- Enrichment functions take models and return models (or new field values). No I/O except where the
  stage is inherently networked (contacts MX lookups, GitHub signals) — and then via `src/core/http.py`.
- Storage is the only layer that knows SQL.

## 2. Layers and modules

| Layer | Module | Responsibility | Phase |
|---|---|---|---|
| core | `config.py` | `Settings` from env vars; `load_yaml()` for `config/` | 1 |
| core | `urls.py` | `canonical_domain()`, `normalize_url()` — dedup building blocks | 1 |
| core | `text.py` | `normalize_text()` shared by geo matching and fingerprints | 1 |
| core | `log.py` | `configure_logging()`; key=value / JSON formatters for event-style logs | 2 |
| core | `robots.py` | RFC 9309 parser (`RobotsRules`) + per-origin cached `RobotsChecker` | 2 |
| core | `http.py` | `HttpClient` (rate limit, cap 5, retries, ETag cache, robots gate), `FetchError` | 2 |
| core | `base.py` | `BaseCollector`, `CollectorResult`, `CollectorIssue`, `run_all` | 2 |
| core | `firecrawl_client.py` | optional JS renderer (`FirecrawlClient`, `HtmlRenderer` protocol) — only with `FIRECRAWL_API_KEY` | 4 |
| models | `enums.py` | `WorkMode`, `Country`, `ContactSourceMethod`, `AtsProvider`, … | 1 |
| models | `startup.py`, `opportunity.py`, `contact.py`, `location.py` | data contracts | 1 |
| models | `collected.py` | `CollectedOpportunity` (startup + opportunity, pre-persistence) | 3 |
| models | `lead.py` | `StartupLead` (news/directory sighting, maybe no website), `lead_name_key` | 4 |
| enrichment | `geo.py` | 3-step location resolver (`GeoResolver`) + haversine helper | 1 |
| enrichment | `internship.py` | `InternshipMatcher` (title + employment-type rules from YAML) | 3 |
| enrichment | `stipend.py` | monthly INR stipend, duration, PPO from text / structured comp | 3 |
| enrichment | `funding.py` | funding-headline parser (`parse_funding_headline`, `detect_stage`) | 4 |
| enrichment | `site.py` | fetch a startup's own pages once (robots, JS-only detection, renderer) | 4 |
| enrichment | `ats_discovery.py` | ATS embeds, cold-pitch phrases (hiring pages only), GitHub orgs | 4 |
| enrichment | `github_signals.py` | org activity from GitHub REST (`GithubSignals`) | 4 |
| enrichment | `scorer.py` | `ScoringConfig` + `HiddenOpportunityScorer` (0–100, explainable) | 4 |
| enrichment | `contacts.py` | 3-pass public contacts; `DnsMxResolver` (MX only, no SMTP) | 4 |
| enrichment | `ats_probe.py` | candidate slugs, `fetch_board`, anti-collision `board_matches`, `infer_domain`, `probe_startup` | 7 |
| core | `search.py` | official search APIs: `BraveSearch`, `FirecrawlSearch`, `search_backend_from_settings` | 7 |
| collectors | `ats_search.py` | `build_queries`, `extract_candidates`, `discover_boards` → `Startup` / `StartupLead` models | 7 |
| collectors | `config.py` | `SourcesConfig`, `CompanySeed`, `AtsSourceConfig` (validated `sources.yaml`) | 3 |
| collectors | `ats_base.py` | `AtsCollector`: filter → geo (+HQ fallback rule) → India filter → terms | 3 |
| collectors | `greenhouse.py`, `lever.py`, `ashby.py`, `workable.py` | provider JSON → `AtsJob` | 3 |
| collectors | `funding_news.py`, `thub.py`, `hn_hiring.py` | Inc42/Entrackr RSS, T-Hub directories, HN hiring thread | 4 |
| collectors | `portal_base.py`, `internshala.py`, `cutshort.py`, `instahyre.py` | Indian student-portal listings → `CollectedOpportunity` (India-only location rule) | C |
| collectors | `remote_base.py`, `himalayas.py`, `remotive.py` | remote feeds → `CollectedOpportunity` open to India (`IndiaOpenness`) | B |
| collectors | `rippling.py`, `breezyhr.py`, `recruitee.py` | more public job-board JSON → `AtsJob` (robots respected) | A |
| collectors | `zohorecruit.py` | Zoho Recruit portal HTML (`input#jobs` JSON) → `AtsJob`; robots respected | v0.2 |
| collectors | `yc_india.py`, `iitm.py`, `nsrcel.py` | YC India directory, IITM portfolio API, NSRCEL popups → `StartupLead` | v0.2 |
| collectors | `registry.py` | `build_collectors()` (all + `inc42`/`entrackr` aliases), `discovered_seeds()` | 3–4 |
| pipeline | `pipeline.py` | `collect_and_store()`, `persist_results()` (+ closing), `enrich_startup()`, `enrich_all()`, `run_scrape()`, `plan_scrape()`, `select_for_enrichment()` | 3–5 |
| exports | `exports.py` | `build_rows()`, `render()` (CSV/JSON, formula-safe), `summarize_stats()` | 5 |
| storage | `db.py`, `tables.py`, `repository.py` | engine/pragmas, SQLModel tables, `Repository` | 1 |
| storage | `filters.py` + `Repository.query_*`, `close_missing_opportunities`, `stats` | `ExportFilter` semantics for exports | 5 |
| cli | `cli.py` | Typer commands `scrape`, `enrich`, `export`, `stats`, `version` (parsing + output only) | 5 |
| ci | `.github/workflows/daily_scrape.yml` | daily scrape → enrich → export, DB/ETag cache, artifacts | 5 |
| ui | `ui/view_models.py` | pure: `UiFilters`→`ExportFilter`, tab rows, labels (₹ Indian grouping), KPI cards, CSV/JSON payloads | 6 |
| ui | `ui/jobs.py` | `scrape_job` / `enrich_job` over `pipeline` with live log lines, `JOB_LOCK`, never raises | 6 |
| ui | `ui/app.py` | NiceGUI layout/events only (the sole `nicegui` importer); `run_ui` binds `127.0.0.1`, `reload=False` | 6 |

## 3. Geolocation engine (`src/enrichment/geo.py`)

`GeoResolver.resolve(raw_location, *, hq_text=None, hq_city=None, source_default_city=None)`
returns a `ResolvedLocation` (`country`, `city_canonical`, `state`, `work_mode`, `lat`, `lng`,
`all_cities`, `resolution_method`, `matched_term`, `raw_location`).

1. **Alias & neighborhood match.** The raw string is normalized (lowercase, punctuation → spaces) and
   scanned with one compiled, longest-first, word-bounded regex built from every city's `aliases` and
   `neighborhoods`. The earliest match is the primary city; all distinct matches go to `all_cities`.
   Weak `state_aliases` (e.g. `Telangana` → Hyderabad) are used only if no city/neighborhood matched.
2. **Work-mode / Remote-India detection.** Explicit `remote_india_phrases` (`remote india`, `remote ist`,
   `wfh`, `anywhere in india`, …) → `remote_india`. A generic remote token plus any India signal
   (city, state, `india`, `IST`, `+5:30`) → `remote_india`. A remote token with no India signal →
   `remote_global` (country `GLOBAL`). Otherwise `hybrid` if a hybrid phrase appears, else `onsite`
   when a city was found. Pure remote-India listings get `city_canonical = "remote_india"`.
3. **HQ fallback.** If still no city (e.g. the post just says `India`): PIN codes
   (`\b[1-8]\d{2}\s?\d{3}\b`, longest-prefix match against `pin_prefixes`) in the raw string, then in
   `hq_text` (careers/contact/footer text) → the startup's known `hq_city` → the source default (e.g.
   T-Hub → `hyderabad`). Only PIN prefixes present in config are trusted.

Config is validated at load: unknown keys fail, and the same alias mapped to two cities raises an error.

## 4. Storage and deduplication (`src/storage/`)

| Table | Natural key (UNIQUE) | Upsert behaviour |
|---|---|---|
| `startups` | `canonical_domain` (`https://www.acme.in/careers` → `acme.in`) | new non-null values win, nulls keep existing |
| `opportunities` | `fingerprint` = sha256(`canonical_domain` · normalized title · normalized URL) | same; `first_seen_at` preserved, `last_seen_at` bumped |
| `contacts` | (`startup_id`, `dedupe_key`) where key = `email:<lower>` or `url:<normalized profile>` | same; keeps most confident method, `verified_mx` sticky |
| `leads` | `name_key` = normalized name minus legal suffixes (`Pvt Ltd`, `LLP`, …) | same; linked to `startups` when a domain is known |

Upserts use SQLite `INSERT … ON CONFLICT DO UPDATE` with `COALESCE(excluded.col, col)`, so running a
scrape twice is a no-op and partial enrichment never erases earlier data. `PRAGMA foreign_keys=ON` and
WAL journaling are set on every connection.

## 5. HTTP politeness layer (`src/core/http.py`, `src/core/robots.py`)

Every request a collector makes goes through `HttpClient` (see ADR 002):

| Concern | Behaviour |
|---|---|
| Per-host pacing | One in-flight request per host; next request waits `uniform(1.5, 2.5)` s after the previous one **finished** (or the robots `crawl-delay`, capped at 60 s, if larger). |
| Global cap | `asyncio.Semaphore(5)` across all hosts. `HttpPolicy` refuses values above 5. |
| Retries | 429, 5xx and transport errors: up to 3 retries, `1s·2ⁿ + jitter` backoff (max 30 s), `Retry-After` honoured (max 120 s). 4xx are returned, not retried. Exhausted → `FetchError`. |
| robots.txt | Checked by default before each request; result cached per origin (24 h; 15 min when unreachable). 2xx parse · 4xx allow all · 429/5xx/network → disallow all. Disallowed → `RobotsDisallowedError`. |
| Opt-out | `respect_robots=False` only for **documented public JSON APIs** (Greenhouse/Lever/Ashby/Workable, HN Algolia, GitHub REST) — with a comment at the call site. Never for HTML pages. |
| Caching | 200 responses with `ETag`/`Last-Modified` are stored (`FileResponseCache` under `SCRAPER_HTTP_CACHE_DIR`); later requests send `If-None-Match`/`If-Modified-Since`; a 304 returns the cached body (`is_from_cache(resp)`). |
| Identity | `User-Agent` from `SCRAPER_USER_AGENT`; its product token selects our robots.txt group. |

Tests inject `clock`, `sleep`, `rng` and `httpx.MockTransport`, so the politeness rules are verified
without real waiting or network access (`tests/conftest.py::make_client`, `FakeTime`).

## 6. How to add a new collector (≈15 minutes)

> Contract defined in `src/core/base.py` (ADR 002). `BaseCollector[TargetT, RawT, ItemT]`:
> `targets()` → `fetch(target)` → `parse(raw, target)`; `run()` is inherited and never raises.

1. **Config:** add the source's seeds to `config/sources.yaml` (slugs, feed URL, etc.).
2. **Fixture:** save an anonymized real response to `tests/fixtures/<source>/<case>.json|html`.
3. **Collector:** create `src/collectors/<source>.py`:
   ```python
   class AcmeBoardCollector(BaseCollector[str, dict, Opportunity]):
       name = "acme_board"  # snake_case, unique; becomes Opportunity.source

       def targets(self) -> list[str]:  # what to fetch, usually from config/sources.yaml
           return ["acme", "examplelabs"]

       async def fetch(self, target: str) -> dict:  # network ONLY via self.http
           # Documented public JSON API -> robots opt-out is allowed (ADR 002).
           return await self.http.get_json(f"https://api.acme-ats.com/{target}", respect_robots=False)

       def parse(self, raw: dict, target: str) -> Iterable[Opportunity]:  # PURE: fixture-testable
           ...
   ```
   `run()` (inherited) fetches all targets concurrently; each target is isolated (timeout
   `target_timeout_s`, default 300 s), and failures become `CollectorIssue`s
   (`stage` = `targets`/`fetch`/`parse`) instead of exceptions. Pass every location string through
   `GeoResolver.resolve()`. A working example lives in `tests/test_base.py::DemoBoardCollector`.
4. **Test:** `tests/test_collector_<source>.py` loads the fixture and asserts on `parse()` output;
   add one `run()` test with `make_client(handler, fake_time)` to cover the fetch path offline.
5. **Register** the collector name for `--sources` in the CLI (Phase 5 registry).
6. Run `uv run ruff check . && uv run pytest`, update `PROGRESS.md`.

## 7. Tier 1 ATS collectors (`src/collectors/`)

Provider modules only map JSON → `AtsJob(title, url, locations, employment_type, workplace,
country_code, description, posted_at, stipend)`. `AtsCollector.parse()` then, per posting:

1. **Internship?** `InternshipMatcher`: title include-regex **or** employment-type field
   (Lever `commitment`, Ashby `employmentType`, Workable `employment_type`, Greenhouse department),
   and no seniority/contract exclusion. Descriptions are never used.
2. **Where?** The workplace hint and an `IN` country code are appended to the location string
   (`"Bangalore - WF (Hybrid) India"`) and resolved by `GeoResolver`. The seed's `hq_city` is used
   **only** when the posting is generic (empty, just "India", or a bare "Remote"), never when it names
   a place we can't map or carries a foreign country code — so "Kuala Lumpur" never becomes Bengaluru.
3. **India only:** postings that resolve outside India are dropped (`india_only=False` to keep them).
4. **Terms:** structured INR monthly compensation (Lever `salaryRange`, Ashby `summaryComponents`)
   wins; otherwise `extract_internship_terms(title + description)`.
5. A malformed posting is logged (`collector.posting_skipped`) and skipped; the board continues.

ATS endpoints are documented public APIs, so `fetch()` passes `respect_robots=False` (ADR 002).
`src/pipeline.py` upserts each `CollectedOpportunity`: startup first (once per domain, stamped
`last_checked_at`), then the opportunity with the returned `startup_id`. See ADR 003.

## 8. Hidden opportunities & contacts (Phase 4)

**Discovery collectors** (all HTML/RSS pages respect robots.txt):

| Collector | Source | Emits |
|---|---|---|
| `funding_news` | Inc42 `/buzz/feed/` + `/startups/feed/`, Entrackr `/rss` | `StartupLead` (stage, amount, date, HQ) from single-company "X raises …" headlines |
| `thub` | Lab 32 TablePress tables; `t-hub.co/startups-directory` (JS-only → Firecrawl or `JavaScriptRequiredError`) | `StartupLead` (website → domain, founders, sector, HQ; Hyderabad only when HQ empty) |
| `hn_hiring` | HN Algolia API, latest "Who is hiring?" | `CollectedOpportunity` for India / Remote-India / worldwide junior roles; **seeker posts skipped** |
| `yc_india` | `ycombinator.com/companies/location/india[/hiring]` `data-page` JSON (the API is robots-disallowed, unused) | `StartupLead` (domain, batch, city, team size, GitHub org, `is_hiring`) |
| `iitm` | `incubation.iitm.ac.in/api/portfolios/active` (paginated, robots respected) | `StartupLead` (website, founders; Chennai only when no city) |
| `nsrcel` | `nsrcel.org/portfolio/` `*-startup-popup` blocks (JS-only → Firecrawl or `JavaScriptRequiredError`) | `StartupLead` (website, founders, sub-sector, stage; Bengaluru default) |

`incubators[].kind` routes each directory to exactly one collector (`tablepress`/`webflow` → `thub`,
`iitm_api` → `iitm`, `nsrcel_popup` → `nsrcel`). `zohorecruit` (ADR 008) is an `AtsCollector` whose
`fetch` reads an HTML page with robots respected. Its seed slug must be a `*.zohorecruit.(in|com)` host.
Keka is detection-only, because its job data is robots-disallowed.

**Enrichment** (`enrich_startup`, per stored startup, fault-isolated in `enrich_all`):

```
fetch_site_pages(/, /careers, /jobs, … ; robots; JS-only -> renderer)
  -> analyze_site: ATS embeds (auto-enroll Tier 1 via discovered_seeds), cold-pitch, GitHub org
  -> fetch_github_signals(org)            [documented API; optional GITHUB_TOKEN]
  -> contacts: Pass 1 direct_public (company-domain emails, person profile links)
               Pass 2 github_public (org email, public members; off-domain emails dropped)
               Pass 3 pattern_mx_verified (founders from leads; only if DNS MX exists)
  -> HiddenOpportunityScorer (scoring.yaml) -> Startup.hidden_score / open_to_cold_pitch / ats / tech_stack
```

Leads without a domain are stored but cannot be enriched until a website is known (ADR 004).

## 9. Adding a city
Append to `cities:` in `config/india_geo.yaml` with `display_name`, `state`, `lat`, `lng`, `aliases`,
`neighborhoods`, `state_aliases`, `pin_prefixes`. Add a case to `tests/test_geo.py`. No Python changes.

## 10. CLI, exports and the posting lifecycle (Phase 5)

```
scraper scrape  ──► pipeline.run_scrape ──► registry.build_collectors(YAML seeds + discovered_seeds(DB))
                                          ──► collect_and_store ──► persist_results ──► close_missing_opportunities
scraper enrich  ──► pipeline.select_for_enrichment ──► enrich_all (EnrichmentContext: DnsMxResolver, Firecrawl?)
scraper export  ──► ExportFilter ──► Repository.query_* ──► exports.build_rows ──► exports.render (csv|json)
scraper stats   ──► Repository.stats ──► exports.summarize_stats (score bands from scoring.yaml)
```

`src/cli.py` only parses/validates flags (cities against `india_geo.yaml`, sources against
`ALL_COLLECTOR_NAMES`, work modes against `WorkMode`) and prints tables. Tests replace
`cli.make_http_client` / `cli.make_mx_resolver` with a mocked transport and a fake resolver.

**Closing vanished postings.** `BaseCollector.close_scope(target)` names a board: the seed's domain
for ATS collectors, `"*"` for the HN thread, `None` (never close) for leads collectors. After a run,
for each board fetched *and* parsed without an issue, open opportunities of that `source` (and
startup) that were not upserted in this run become `closed`. A board that failed, or a result
with any persistence error, closes nothing. A closed posting that reappears is reopened.

**Filter semantics** (`ExportFilter`): `(city ∈ cities AND work_mode ∈ modes) OR
(include_remote AND work_mode = remote_india)`; `min_hidden_score` filters on the startup's score
(rows without a scored startup drop out); `status` defaults to `open`. For startups/contacts,
cities match `hq_city`, and `--include-remote` / `--work-mode` mean "has an open role of that mode".

## 11. Repository layout

```
.github/workflows/daily_scrape.yml   daily automation (ADR 005)
config/                              india_geo.yaml · sources.yaml · scoring.yaml
docs/ARCHITECTURE.md, docs/ADRs/     design + decisions 001–011
src/core/                            config, text/urls, log, http, robots, base, firecrawl_client
src/models/                          startup, opportunity, contact, lead, location, collected, enums
src/collectors/                      ATS (greenhouse, lever, ashby, workable, rippling, breezyhr,
                                     recruitee, zohorecruit via ats_base),
                                     funding_news, thub, hn_hiring, yc_india, iitm, nsrcel,
                                     remote_base, himalayas, remotive,
                                     portal_base, internshala, cutshort, instahyre,
                                     ats_search, config (schema), registry
src/enrichment/                      geo, internship, stipend, funding, site, ats_discovery,
                                     github_signals, scorer, contacts
src/storage/                         db, tables, repository, filters
src/pipeline.py · src/exports.py · src/cli.py
src/ui/                              optional NiceGUI dashboard (extra `ui`; ADR 006)
tests/ + tests/fixtures/             offline suite (network blocked in conftest)
data/ · exports/                     runtime output (git-ignored)
```

## 12. Optional local dashboard (Phase 6, ADR 006)

```
scraper ui ──► src/ui/app.run_ui(settings, host=127.0.0.1, port=8080, reload=False)
page load  ──► view_models.{kpi_cards, internship_rows, hidden_opportunity_rows, contact_rows}
                 └─► Repository.query_* / stats()  (local SQLite only — no network)
download   ──► view_models.download_payload ──► exports.rows_to_csv | rows_to_json ──► ui.download.content
Run scrape / Run enrich (explicit click) ──► ui/jobs.py ──► pipeline.run_scrape | enrich_all
```

Layering: `ui → {pipeline, exports, storage (Repository, ExportFilter), enrichment.geo/scorer, models}`.
`src/ui` never imports `httpx`, SQLAlchemy/SQLModel, `sqlite3` or `src.storage.{tables,db,repository}`
(enforced by `tests/test_ui_helpers.py`); only `src/ui/app.py` imports `nicegui`, so everything else
runs and is tested in CI without the extra. Launch: `uv sync --extra ui` then `uv run scraper ui`.

## 13. Automatic ATS seed discovery (ADR 007)

```
enrich_startup ──(no ats_provider and no embed found)──► ats_probe.probe_startup
      candidate_slugs(domain, name) × providers ──► fetch_board (public JSON API)
      ──► board_matches (domain link OR exact name) ──► Startup.ats_provider / ats_slug  (SQLite)

scraper discover ──► search_backend_from_settings (Brave | Firecrawl | none → hint, 0 calls)
      ──► ats_search.build_queries (sources.yaml: ats_search) ──► backend.search
      ──► extract_candidates (find_ats on result URLs) ──► skip known (YAML seeds + DB)
      ──► fetch_board ──► infer_domain ──► pipeline.run_discover:
             domain found  → upsert Startup (discovered_via="ats_search")   [conflict → not overwritten]
             no domain     → upsert StartupLead (source="ats_search")
next `scraper scrape` ──► discovered_seeds(repo.list_startups()) polls every stored board
```

`config/sources.yaml` is configuration only (templates, thresholds); automated runs never write it.

## 14. India ATS portals & regional directories (ADR 008)

```
scraper scrape ──► zohorecruit: GET https://{host}/jobs/Careers (robots) ──► input#jobs JSON ──► AtsCollector.parse
               ──► yc_india:    /companies/location/india/hiring?page=N, then /india?page=N (robots) ──► StartupLead
               ──► iitm:        /api/portfolios/active?page=N&limit=100 (robots) ──► StartupLead (default chennai)
               ──► nsrcel:      /portfolio/ popups (robots; Firecrawl if JS-only) ──► StartupLead (default bengaluru)
persist_results ──► leads with a domain → Startup (team_size, github_org) ──► `enrich` finds ATS + contacts
```

Storage: `leads` gained nullable `batch`, `is_hiring`, `team_size` and `github_org`.
`Repository.create_schema()` adds missing nullable columns to older database files (additive only).

## 15. Expanded public ATS providers (ADR 009)

| Provider | Endpoint | robots.txt |
|---|---|---|
| `rippling` | `api.rippling.com/platform/api/ats/v1/board/{slug}/jobs` | respected (`respect_robots = True`) |
| `breezyhr` | `https://{slug}.breezy.hr/json` | respected (per-tenant robots) |
| `recruitee` | `https://{slug}.recruitee.com/api/offers/` | respected (per-tenant robots) |
| smartrecruiters | — (API robots.txt: `Disallow: /` for `*`) | **non-target**, detection only |

There are two provider sets:
- `JSON_ATS_PROVIDERS` (Tier 1 plus the three above) can be probed and searched.
- `POLLED_ATS_PROVIDERS` adds Zoho, and is what gets polled and auto-enrolled.

`ats_probe.ROBOTS_OPT_OUT` limits the ADR 002 robots opt-out to Greenhouse, Lever, Ashby and Workable.

## 16. Remote job feeds (ADR 010)

```
himalayas: robots.txt ──► GET /jobs/api/search?{query}   (each sources.yaml query, page 1 only)
remotive:  robots.txt ──► GET /remote-jobs/feed (RSS; /api/* is robots-disallowed, unused)
  ──► InternshipMatcher(title, employmentType/type)
  ──► IndiaOpenness: India-restricted | IST-only → remote_india ; worldwide → remote_global ; else drop
  ──► stipend.py (Himalayas INR monthly salary, else text) ──► CollectedOpportunity(startup=None)
```

Each collector has a single target with `close_scope = "*"`, so closing happens only after a fully
successful read. The rules live in `sources.yaml: remote_feeds` (`india_country_names`,
`india_utc_offset`, `worldwide_keywords`, `keep_worldwide`).

## 17. Indian student portals (ADR 011)

```
internshala: robots ──► /internships/internship-in-{city}/ [+ page-N/] , /internships/work-from-home-internships/
cutshort:    robots ──► /jobs/startup-jobs-in-{city} , /jobs/remote-jobs   (page 1; __NEXT_DATA__ jobs)
instahyre:   robots ──► /internship-jobs/ (script#initial-job-search) ──► its own meta.next links
  ──► InternshipMatcher (+ portal type: card kind / roleTypes / job_type=2)
  ──► portal_base.india_portal_location: India-only portal → remote text = remote_india; foreign → drop
  ──► stipend.py on listing text ──► CollectedOpportunity (Cutshort: Startup from published website)
```

The portals' Terms prohibit automated access, and the owner decided to collect anyway (ADR 011).
`--cities` narrows the city targets, and `remote` is always kept. Unstop (CloudFront 403) and Cuvette
(DNS) are non-targets.
