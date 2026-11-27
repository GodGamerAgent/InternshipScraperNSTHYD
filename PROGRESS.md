# PROGRESS.md — Build Log & Resume Point

> Updated after **every** completed sub-step so any AI or human can resume cold.
> Source of truth for scope: `context.md` (why) and `implementation.md` (how). Rules: `AGENTS.md`.

## Current: Automatic ATS Seed Discovery (ADR 007) — ✅ COMPLETE (awaiting user review)
User chose (2026-11-26): build this first; v0.2 (Indian ATS portals & directories) is PAUSED and will use ADR 008.

| # | Step | Status |
|---|------|--------|
| A.0 | Config: `ats_probe:` + `ats_search:` in sources.yaml (+ schemas), `BRAVE_SEARCH_API_KEY` in Settings/.env.example | ✅ done |
| A.1 | `src/enrichment/ats_probe.py` — candidate slugs, board snapshot, anti-collision, domain inference | ✅ done (live: groww accepted by name; atlan accepted for atlan.com, rejected for atlan.in) |
| A.2 | Probe wired into `enrich_startup` (only when no ATS + no embed found) | ✅ done (`ats_probe.enabled` toggle; report shows `provider:slug (probed)`) |
| A.3 | `src/core/search.py` (Firecrawl /search, Brave) + `src/collectors/ats_search.py` | ✅ done (models only; lead fallback when domain unresolved) |
| A.4 | `pipeline.run_discover`, `scraper discover`, NiceGUI "Discover Boards" | ✅ done (no-key → hint, exit 0, 0 network calls; conflict guard) |
| A.5 | `tests/test_ats_discovery_auto.py` | ✅ done (35 tests; 531 total; found + fixed prefix-name collision bug) |
| A.6 | ADR 007, README, ARCHITECTURE, AGENTS, final checks | ✅ done |

**Files:** new `src/enrichment/ats_probe.py`, `src/core/search.py`, `src/collectors/ats_search.py`,
`tests/test_ats_discovery_auto.py`, `tests/fixtures/ats_search/{brave,firecrawl_v1,firecrawl_v2}.json`,
`docs/ADRs/007-automatic-ats-seed-discovery.md`; modified `src/pipeline.py` (probe in `enrich_startup`,
`known_boards`, `run_discover`, `DiscoverReport`), `src/cli.py` (`discover`), `src/ui/jobs.py` (`discover_job`),
`src/ui/app.py` (Discover boards button), `src/core/firecrawl_client.py` (`search`), `src/core/config.py`
(`brave_search_api_key`), `src/collectors/config.py` (`AtsProbeConfig`, `AtsSearchConfig`), `config/sources.yaml`
(`ats_probe:`, `ats_search:` — config only), `.env.example`, `README.md`, `docs/ARCHITECTURE.md` (§13), `AGENTS.md`.

**Last verified checks (2026-11-26):** `ruff check` ✅ · `ruff format --check` ✅ (98 files) · `pytest` 531 passed
(with ui extra) / 530 passed + 1 skipped (CI-like, no extra) · `scraper discover --dry-run` ✅ · no-key path: hint,
exit 0, 0 network calls.

**Bug found by tests and fixed:** prefix name matching accepted "Atlan Travel Group" / "Hyd Stack Furniture" boards
→ exact match after legal-suffix + `name_noise` removal (+ `software`, `solutions`, `services`, `systems` noise).

**Next exact step:** wait for user review; then resume v0.2 (Indian ATS portals & directories) as ADR 008 from the
"Paused: v0.2" notes below.

Notes: sources.yaml now has 37 user-added verified seeds; `test_real_seeds_are_valid_and_verified` upper bound
(≤25) removed (kept ≥10 + verified_on). Boards whose company domain can't be inferred are saved as `StartupLead`
(not polled) rather than under an invented domain. Search backends are mock-tested only (no API keys available).

---

## Current: Batch C — Indian Student Portals (ADR 011) — ✅ COMPLETE (awaiting user final review)

Pre-flight: feasible as an audit. I **cannot see** my remaining usage limit. User decision (2026-11-26): "If Public access is there go for it", so the portals with unauthenticated access are built despite their Terms.

| # | Step | Status |
|---|------|--------|
| C.0 | Compliance audit: robots.txt, unauthenticated access, bot protection, plus each site's Terms on automated access | ✅ done |
| C.1 | User decided: build Internshala, Cutshort, Instahyre; Unstop (403) and Cuvette (DNS) are non-targets | ✅ done |
| C.2 | `src/collectors/{portal_base,internshala,cutshort,instahyre}.py`, `student_portals` config, registry; fixtures + `tests/test_collectors_portals.py` (15) + CLI dry-run case | ✅ done |
| C.3 | ADR 011, README, ARCHITECTURE §17, AGENTS §4 · ruff ✅ · format ✅ · pytest **601 passed** (2026-11-26) | ✅ done |

**Audit (2026-11-26).** Our User-Agent, robots.txt first, at most one listing page per site plus its Terms page. Raw pages
are in the scratchpad only.

| Portal | robots.txt | Unauthenticated access | Bot protection | Terms of Service | Recommendation |
|---|---|---|---|---|---|
| Internshala | Listing pages `/internships/…` and `/page-N/` allowed. Disallowed: `/internship/details/`, `/internship/search/`, `/api/`, any `?query` | 200; 50 server-rendered cards per page | None seen (nginx + CloudFront; reCAPTCHA only on login/apply forms) | **Prohibits** "automated systems, software, bots, spiders, scripts… to access, extract, scrape, crawl" ("including but not limited to" AI training) | **No-go (ToS)** |
| Unstop | Allows `/internship/`, `/job/`, `/api/public/*`; disallows `/api/*` | **403 Forbidden** from CloudFront on `/internships` for our UA | **WAF block** | Not checked (already blocked) | **No-go (bot block)** |
| Cuvette | — | **Unreachable:** `cuvette.tech` has NS records but **no A record** (DNS does not resolve) | — | — | **No-go (unreachable)**; re-audit if a new domain is confirmed |
| Cutshort | Listing pages allowed; disallows `/view/j/`, `/vj/`, `/apply/`, `/*?job_listing` | 200; 50 jobs in server-side `__NEXT_DATA__` (few internships; mostly full-time) | Cloudflare, no challenge served; reCAPTCHA badge for forms | **Prohibits** automated access, bots, scraping, data extraction | **No-go (ToS)** |
| Instahyre | `User-agent: *` with no rules (all allowed) | 200, but the page is a JS template. `/api/v1/job_search/` returns JSON **without login** | Cloudflare, no challenge served | **Prohibits** "automated software… to crawl or spider any part of the Platform… harvest or scrape" | **No-go (ToS)** |

**Live check on the probe pages:**
- Internshala: 154 internships from 3 pages; 127 with a stipend, 152 with a duration.
- Cutshort: 10 remote internships, all with company domains.
- Instahyre: 18 of 20.

**Next exact step:** the user's final review of Batch C. All requested batches (v0.2, A, B, C) are done.

**Resume Prompt:**

> Read AGENTS.md, context.md, implementation.md, docs/ARCHITECTURE.md and PROGRESS.md. v0.2 and Batches A–C (ADRs
> 008–011) are complete and green (601 passed). Wait for the user's next request.

---

## Batch B — Remote Job Feeds (ADR 010) — ✅ COMPLETE (user approved 2026-11-26)

Pre-flight: feasible. I **cannot see** my remaining usage limit. User approved v0.2 + Batch A; Keka and SmartRecruiters
stay detection-only.

| # | Step | Status |
|---|------|--------|
| B.1 | Polite probes (robots.txt first, our UA): Himalayas, Remotive | ✅ done |
| B.2 | `remote_feeds` config + shared India-openness rules (`src/collectors/remote_base.py`) | ✅ done |
| B.3 | `src/collectors/himalayas.py` (JSON search API), `src/collectors/remotive.py` (RSS) | ✅ done |
| B.4 | Registry (`REMOTE_COLLECTORS`) + fixtures `tests/fixtures/{himalayas,remotive}/` + `tests/test_collectors_remote.py` (12) + CLI dry-run case | ✅ done |
| B.5 | ADR 010, README, ARCHITECTURE §16 · ruff ✅ · format ✅ · pytest **585 passed** (2026-11-26) | ✅ done |

**Probe findings (2026-11-26):**
- **Himalayas.**
  - **robots.txt:** `Allow: /`, except `/apply` and any `?page=` / `&page=` on `/jobs*`.
  - **Usable endpoints:** the public keyless JSON API is documented at himalayas.app/api. `GET /jobs/api/search?country=IN&employment_type=Intern&sort=recent`
    returns 166 matches, 20 per page, and later pages need `&page=N`, which robots.txt disallows. So only page 1 of each query is fetched.
    `worldwide=true&employment_type=Intern` returns 3. The browse feed `/jobs/api` uses `cursor=`, which is allowed but unfiltered
    (116k jobs) and is not used.
  - **Fields:** `title, companyName, companySlug, employmentType ("Intern"), minSalary, maxSalary, currency,
    salaryPeriod ("monthly"), locationRestrictions [country names; [] = worldwide], timezoneRestrictions [UTC offsets; 5.5 = IST],
    description (HTML), pubDate (epoch s), expiryDate, applicationLink, guid`.
  - **API terms:** use `applicationLink` unchanged and show himalayas.app as the source.
- **Remotive.**
  - **robots.txt:** `Disallow: /api/*`, so the JSON API is skipped (a non-target). `/v2/feed` and `/remote-jobs/digest/feed` are also disallowed.
    `/remote-jobs/feed` (RSS) is allowed. `/remote-jobs/feed/{category}` returned 404.
  - **RSS items (17 latest):** `title, company, location (free text: "Worldwide", "USA", "LATAM, Europe, …"), type
    (full_time/contract/part_time/freelance/internship), link, pubDate, category, description`. There were 0 internships on probe day.

**Live check:** run offline over the probe samples, 21 of 23 Himalayas interns were kept. They resolved to remote_india or
remote_global, and INR monthly stipends were parsed. Remotive RSS had 0 internships on the probe day.

**Files.**
- **Created:**
  - `src/collectors/{remote_base,himalayas,remotive}.py`
  - `docs/ADRs/010-remote-job-feeds.md`
  - `tests/test_collectors_remote.py`
  - 2 fixture dirs
- **Modified:**
  - `src/collectors/{config,registry}.py`: `RemoteFeedsConfig`, `HimalayasConfig`, `RemotiveConfig`.
  - `config/sources.yaml`: the `remote_feeds` section.
  - `tests/test_cli.py`
  - `tests/test_collectors_discovery.py`
  - README, ARCHITECTURE

**Next exact step:** wait for user review. Then Batch C: run the mandatory compliance audit first (robots.txt, unauthenticated
access, bot protection) for Internshala, Unstop, Cuvette, Cutshort and Instahyre, and report a 5-row go/no-go table to the user
before writing any collector. The next ADR is 011.

**Resume Prompt:**

> Read AGENTS.md, context.md, implementation.md, docs/ARCHITECTURE.md and PROGRESS.md. v0.2, Batch A and Batch B
> (ADRs 008–010) are complete and green (585 passed). If the user approved Batch B, start Batch C with the audit only:
> robots.txt + unauthenticated public access + bot protection for Internshala, Unstop, Cuvette, Cutshort and Instahyre.
> Report the go/no-go table and stop before writing collectors.

---

## Batch A — Public Global ATS APIs (ADR 009) — ✅ COMPLETE (user approved 2026-11-26)

Pre-flight: feasible. I **cannot see** my remaining usage limit. ADR is **009** because 008 is v0.2. Batch B will only
add Himalayas + Remotive (YC India / IITM / NSRCEL shipped in v0.2).

| # | Step | Status |
|---|------|--------|
| A.1 | Polite probes (robots.txt first, our UA, raw samples in scratchpad only) + seed verification | ✅ done |
| A.2 | `src/collectors/{rippling,breezyhr,recruitee}.py` (`AtsCollector`, `respect_robots = True`) | ✅ done |
| A.3 | `AtsProvider` (+rippling, breezyhr, recruitee, smartrecruiters), `JSON_ATS_PROVIDERS`, sources.yaml (seeds, embed hosts, `ats_probe.providers`, `ats_search.queries`), registry, `ats_discovery` patterns, `ats_probe` (`BOARD_URLS`, `ROBOTS_OPT_OUT`, board names), CLI `discover --provider` | ✅ done |
| A.4 | Fixtures `tests/fixtures/{rippling,breezyhr,recruitee}/` + `tests/test_collectors_ats_batch_a.py` (18 tests) | ✅ done |
| A.5 | ADR 009, README, ARCHITECTURE §15, AGENTS §4 note · ruff ✅ · format ✅ · pytest **572 passed** (2026-11-26) | ✅ done |

**Findings:**
- **SmartRecruiters is skipped.** `api.smartrecruiters.com/robots.txt` has `User-agent: *` → `Disallow: /`; only
  LinkedInBot is allowed. It is detection-only and recorded in the ADR as a non-target.
- **Tier 1 re-check.** Each Tier 1 API path is allowed by its host's robots.txt, so there is no conflict with the new gate.
- **Seeds (11).**
  - Rippling (2): Rippling, Azira.
  - BreezyHR (5): 75F, Anovia, sa.global, NationsBenefits, SkySpecs.
  - Recruitee (4): Lenskart, TransPerfect, MGID, Framestore.
- **Rippling below target.** The batch asked for 3–5 Rippling seeds. TYLsemi, OneOrigin and Genios AI have Indian roles,
  but their domains couldn't be verified, so they were omitted rather than guessed.
- **Few internships.** Most seeds are global companies' India offices; internship counts were low on 2026-11-26.

**Files.**
- **Created:**
  - `src/collectors/{rippling,breezyhr,recruitee}.py`
  - `docs/ADRs/009-expanded-ats-providers.md`
  - `tests/test_collectors_ats_batch_a.py`
  - 3 fixture dirs
- **Modified:**
  - `src/models/enums.py`
  - `src/collectors/{config,ats_base,registry}.py`
  - `src/enrichment/{ats_probe,ats_discovery}.py`
  - `src/cli.py`
  - `config/sources.yaml`
  - `tests/test_{collectors_ats,collectors_discovery,config}.py`
  - README, ARCHITECTURE, AGENTS

**Next exact step:** wait for user review of v0.2 + Batch A (and the Keka decision). Then Batch B: probe Himalayas +
Remotive robots.txt / public API or RSS, then build India / IST / worldwide-remote internship collectors.

**Resume Prompt:**

> Read AGENTS.md, context.md, implementation.md, docs/ARCHITECTURE.md and PROGRESS.md. v0.2 (ADR 008) and Batch A
> (ADR 009) are complete and green (572 passed). If the user approved, start Batch B: probe Himalayas + Remotive
> politely (robots.txt first), then implement remote-feed collectors that keep only internships open to India
> (India / IST / worldwide remote) via InternshipMatcher + GeoResolver. Next ADR is 010.

---

## v0.2 — Indian ATS Portals & Regional Startup Directories (ADR 008) — ✅ COMPLETE (user approved 2026-11-26; Keka stays detection-only)

| # | Step | Status |
|---|------|--------|
| 7.1 | Polite probes: robots.txt + live shapes (Keka, Zoho Recruit, YC India, IITM, NSRCEL) + seed verification | ✅ done |
| 7.2 | `src/collectors/zohorecruit.py` + 5 verified seeds (Keka: robots-blocked → needs user decision) | ✅ done |
| 7.3 | `src/collectors/yc_india.py` (+ lead columns batch / is_hiring / team_size / github_org + migration) | ✅ done |
| 7.4 | `src/collectors/iitm.py` (chennai) + `src/collectors/nsrcel.py` (bengaluru) | ✅ done |
| 7.5 | Registry, `discovered_seeds` for zohorecruit, `ALL_COLLECTOR_NAMES`, fixtures + offline tests | ✅ done |
| 7.6 | ADR 008, README, ARCHITECTURE (§14), AGENTS §2 note | ✅ done |
| 7.7 | ruff ✅ · format ✅ · pytest **554 passed** (2026-11-26) | ✅ done |

**Probe findings (2026-11-26; raw samples kept in the session scratchpad only, never committed):**
- **Keka — BLOCKED by robots.txt.** Every probed tenant serves `Disallow: /` + `Allow: /careers`; `/careers` is a JS
  shell and the job data loads from `/ats/...` (disallowed). No documented public API. No collector built (AGENTS §4:
  never work around robots). Careers-page detection of Keka slugs (`ats_discovery`) keeps working. Needs user decision.
- **Zoho Recruit:** `https://{portal-host}/jobs/Careers` is robots-allowed (Zoho robots disallows only authenticated
  paths) and server-renders all published jobs as JSON in `<input type="hidden" id="jobs" value="[…]">` (fields
  `id, Posting_Title, City, State, Country, Remote_Job, Job_Type, Job_Description (HTML), Date_Opened, Publish`).
  Job URL `https://{host}/jobs/Careers/{id}`. Verified seeds (company domain linked from the portal itself):
  acciorobotics.zohorecruit.com → acciorobotics.com, crossbowlabs.zohorecruit.com → crossbowsec.com,
  drinkprime.zohorecruit.in → drinkprime.in, kratikal.zohorecruit.com → kratikal.com,
  rareminds.zohorecruit.in → rareminds.in. Not seeded (no own-domain link on portal): joinswitchon, auxoai, citygreens.
- **YC India:** `api.ycombinator.com` is robots-disallowed (not used). `https://www.ycombinator.com/companies/location/india?page=N`
  and `/companies/location/india/hiring?page=N` are robots-allowed; `data-page` JSON → `props.companies[]`
  (`name, slug, website, batch_name, city, country, team_size, github_url, tags, one_liner, ycdc_status`) +
  `totalPages`/`hasMore`. 177 India companies (4 pages), 52 hiring (2 pages). Hiring indicator = present on the
  `/hiring` listing (no per-company flag exists).
- **IITM Incubation Cell:** `https://incubation.iitm.ac.in/api/portfolios/active?page=N&limit=100` (JSON; robots.txt is
  an SPA fallback with no rules → allowed). 425 portfolios / 5 pages; `company_name, website_url, founders[].name,
  tagline, location (["India"] only), incubated_year`. No city field → `default_city: chennai`.
- **NSRCEL (IIM Bangalore):** `https://nsrcel.org/portfolio/` robots-allowed; 23 `.fintech-coe-startup-popup`
  containers: `img[alt]` = name, `<p>` = description, `<li>` "Key: Value" (Founder, Co-Founders, Stage of Venture,
  Sub-Sectors, Website). No HQ field → `default_city: bengaluru`.

**v0.2 files.**
- **Created:**
  - `src/collectors/{zohorecruit,yc_india,iitm,nsrcel}.py`
  - `docs/ADRs/008-india-ats-and-directories.md`
  - `tests/test_collectors_{zohorecruit,yc_india,incubators}.py`
  - `tests/fixtures/{zohorecruit,yc_india,incubators}/`
- **Modified:**
  - `config/sources.yaml`: `ats.zohorecruit`, 2 new incubators, `yc_india`.
  - `src/collectors/config.py`: `POLLED_ATS_PROVIDERS`, `ZOHO_PORTAL_HOST`, incubator kinds and paging, `YcDirectoryConfig`.
  - `registry.py`: `DEFAULT_COLLECTOR_NAMES`; Zoho in `discovered_seeds`.
  - `thub.py`: kinds filter; public `website_domain` / `FOUNDER_SPLIT`.
  - `enrichment/ats_discovery.py`: Zoho full-host slug, `pollable`.
  - `models/lead.py` and `storage/tables.py`: 4 nullable columns.
  - `storage/repository.py`: additive `_add_missing_columns`.
  - `exports.py`: lead columns.
  - Tests updated for the new registry.
  - README, ARCHITECTURE, AGENTS.

**Open decision for the user:** Keka. Its job data is robots-disallowed, so no collector was built and detection only is
recommended. The full options are in ADR 008 §2.


---

## Phase 6 — Optional Local NiceGUI Web UI — ✅ COMPLETE (user approved)

| # | Step | Status |
|---|------|--------|
| 6.0 | Streamlit attempt fully reverted (0 refs); `nicegui>=2.0` as `[project.optional-dependencies] ui` (4.1.0) | ✅ done |
| 6.1 | `src/ui/view_models.py` (pure) + public `rows_to_csv` / `rows_to_json` in `src/exports.py` | ✅ done |
| 6.2 | `src/ui/jobs.py` — async scrape/enrich wrappers, live log lines, `JOB_LOCK`, never raises | ✅ done |
| 6.3 | `src/ui/app.py` (NiceGUI) + `scraper ui [--host 127.0.0.1] [--port 8080] [--open/--no-open]` | ✅ done |
| 6.4 | `tests/test_ui_helpers.py` — 39 offline tests (view models, payloads, jobs, CLI `ui`, layering guard) | ✅ done |
| 6.5 | Visual check (fictional demo DB): all 4 tabs, KPIs, city filter + Include Remote India, theme modes; all page requests local-only | ✅ done |
| 6.6 | ADR 006, README "Local dashboard", ARCHITECTURE §2/§11/§12, AGENTS UI rules; final checks | ✅ done |

Bug found in the visual check and fixed: `ui.dark_mode()` defaulted to *light* and Quasar's light body is
transparent, so a dark browser canvas made tab labels invisible → theme toggle Auto/Light/Dark (Auto = OS) plus
explicit page backgrounds. The temporary `.claude/launch.json` (pointed at a scratchpad demo DB) was removed.
The Download button was not clicked in the browser (it would save a file to your Downloads); payloads are tested.

## Phase 6 files created / modified
- **New:** `src/ui/__init__.py`, `src/ui/view_models.py`, `src/ui/jobs.py`, `src/ui/app.py`,
  `tests/test_ui_helpers.py`, `docs/ADRs/006-local-nicegui-ui.md`.
- **Modified:** `pyproject.toml` + `uv.lock` (`ui` extra), `src/exports.py` (`rows_to_csv`, `rows_to_json`),
  `src/cli.py` (`ui` command, wildcard-host refusal), `README.md`, `docs/ARCHITECTURE.md`, `AGENTS.md`.

## Last verified checks (2026-11-26)
```
$ uv run ruff check .          -> All checks passed!
$ uv run ruff format --check . -> 93 files already formatted
$ uv run pytest                -> 496 passed (with ui extra)
$ uv sync --frozen && uv run pytest -> 495 passed, 1 skipped (CI-like, no ui extra; nicegui smoke test skipped)
```

**NiceGUI facts (4.1.0):** `ui.run` host defaults to **0.0.0.0** and `reload=True` → `run_ui` always passes
`host="127.0.0.1"` (or the explicit `--host`) and `reload=False`. `ui.download.content(bytes|str, filename,
media_type)`. An exact `uv sync` (CI) removes the extra; `uv run` keeps it.

## Resume Prompt (maintenance / v0.2)
> You are a Senior Python Data Engineer maintaining the India Startup Internship Scraper (Phases 1–6 complete). Read
> `context.md`, `implementation.md`, `AGENTS.md`, `docs/ARCHITECTURE.md`, ADRs 001–006 and `PROGRESS.md` first, then
> run `uv run ruff check . && uv run ruff format --check . && uv run pytest` (expect 496 passed with `--extra ui`,
> 495 + 1 skipped without). Pick one item from "Known follow-ups", work in small green sub-steps, update
> `PROGRESS.md` after each, and stop for review.

---

## Phase 5 — CLI, Exports & Daily Automation — ✅ COMPLETE (user approved 2026-11-26)

| # | Step | Status |
|---|------|--------|
| 5.0 | Re-verify state (425 passed) and re-read CLI/repository/pipeline/registry | ✅ done |
| 5.1 | Storage: `ExportFilter`, `query_*`, `close_missing_opportunities`, `stats`; `BaseCollector.close_scope` | ✅ done |
| 5.2 | Pipeline: closing in `persist_results`, `run_scrape`/`plan_scrape`/`select_for_enrichment`; `src/exports.py`; registry `extra_seeds`/`cities` | ✅ done |
| 5.3 | `src/cli.py`: `scrape` (+`--dry-run`), `enrich`, `export`, `stats`, global `--db/--config-dir/--log-level` | ✅ done |
| 5.4 | Offline CLI tests (`tests/test_cli.py`, CliRunner + temp config/DB + mocked HTTP/DNS) | ✅ done |
| 5.5 | `.github/workflows/daily_scrape.yml` + static checks (`tests/test_workflow.py`) | ✅ done |
| 5.6 | Docs (README, ARCHITECTURE §10–11, AGENTS), ADR 005, final checks, dry-run, handoff checklist | ✅ done |

Mutation-checked (each deliberate bug caught by a test, then reverted): never closing after a partial persist,
`--include-remote` OR, closing only unseen postings.
**Incident (fixed):** after the mutation check, `src/pipeline.py` came back with the mutated line
(`set().add(` instead of `seen_ids.add(`) — most likely an open editor saving its buffer. Caught by the final full
run, restored, and every file touched by any mutation check (Phases 2–5) was grepped clean. Also fixed a test leak:
the CLI's logging handler outlived `CliRunner` in `test_version` (now an autouse restore fixture). Suite passes in
forward and reverse file order.

Note: `study.html` in the project root was not created by the build sessions — left untouched.

## Phase 5 files created / modified
- **New:** `src/exports.py`, `src/storage/filters.py`, `.github/workflows/daily_scrape.yml`,
  `docs/ADRs/005-cli-exports-automation.md`, `tests/test_queries.py`, `tests/test_exports.py`, `tests/test_cli.py`,
  `tests/test_workflow.py`.
- **Modified:** `src/cli.py` (all commands), `src/pipeline.py` (closing, `PersistReport.closed`, CLI entry points),
  `src/storage/repository.py` (`close_missing_opportunities`, `query_opportunities/startups/leads/contacts`,
  `open_opportunity_counts`, `stats`), `src/storage/__init__.py`, `src/core/base.py` (`close_scope`,
  `CollectorResult.closable_scopes`), `src/collectors/{ats_base,hn_hiring}.py` (`close_scope`),
  `src/collectors/registry.py` (`cities`, `extra_seeds` passthrough), `pyproject.toml` (ruff B008 allow-list for
  `typer.Option`), `README.md`, `docs/ARCHITECTURE.md`, `AGENTS.md`.

## Last verified checks (2026-11-26)
```
$ uv run ruff check .          -> All checks passed!
$ uv run ruff format --check . -> 87 files already formatted
$ uv run pytest                -> 458 passed in 3.17s   (also 458 passed with test files in reverse order)
$ uv run scraper scrape --dry-run --sources greenhouse,thub,hn_hiring --city hyderabad
  greenhouse (1 targets): highradius / thub (2 targets): thub_lab32, thub / hn_hiring (1 targets): latest_thread
```
All workflow `export`/`stats` commands were also run against an empty scratch DB (no network) successfully.

## Handoff checklist for future developers
1. **Setup:** `uv sync` → `uv run pytest` (458 offline tests) → `uv run scraper --help`.
2. **First real run:** `cp .env.example .env`, set `SCRAPER_USER_AGENT` with a real contact URL, then
   `uv run --env-file .env scraper scrape` → `scraper enrich --limit 10` → `scraper export --cities hyderabad --include-remote`.
3. **Git + CI (not done yet — folder is not a git repo):** `git init`, commit (`.gitignore` already excludes `.env`,
   `data/`, `exports/`), push to GitHub, then set repo variable `SCRAPER_USER_AGENT`; optional secrets
   `SCRAPER_GITHUB_TOKEN`, `FIRECRAWL_API_KEY`; `EXPORT_CONTACTS=true` **only if the repo is private**. Trigger
   "Daily scrape" via *Run workflow* once and check the artifact. The workflow is statically tested but has never
   executed on GitHub.
4. **Add an ATS seed:** verify the board on the public API, add `{name, slug, domain, hq_city?, verified_on}` under
   `config/sources.yaml: ats.<provider>.companies`; `uv run pytest tests/test_sources_config.py`.
5. **Add a city:** `config/india_geo.yaml` block + a `tests/fixtures/geo/location_cases.json` case.
6. **Add a collector:** `docs/ARCHITECTURE.md` §6 (targets/fetch/pure parse, `close_scope` if it emits opportunities),
   fixture + test, register in `src/collectors/registry.py`.
7. **Read before touching:** `AGENTS.md` (compliance + layering), ADRs 001–005, "Gotchas" below.
8. **Verify Firecrawl once:** the T-Hub Webflow card selectors are assumptions until run with a real key.

## Known follow-ups (not done)
- Leads without a website can't be enriched; consider a manual "attach domain" command.
- Actions cache is not durable storage (evicted after 7 days unused); long-term history needs external storage.
- Cross-domain redirects skip a robots re-check for the new host (ADR 002).
- `tech_stack` from job descriptions is not extracted (only GitHub languages).

## Gotchas (all phases)
- SQLModel 0.0.50 `UTCDateTime` **rejects naive datetimes** — always pass aware UTC.
- SQLAlchemy `JSON` needs `none_as_null=True` for COALESCE merges on list columns.
- `"unknown"` city is stored as NULL so it never overwrites a resolved city on upsert.
- Spec files are lowercase on disk (`context.md`, `implementation.md`) — matters on case-sensitive Linux CI.
- Fixtures must be anonymized (`*.example` domains, fictional people). Real probe samples stay out of the repo.
- Layering: `core` imports only `core`; `enrichment` never imports `collectors`; only `src/pipeline.py` mixes
  collectors/enrichment with storage; `src/cli.py` is parsing + output only.
- The CLI reconfigures root logging; CLI tests must restore it (autouse fixture in `tests/test_cli.py`).
- After any mutation check, **grep the mutated lines again** — an open editor can write the mutated buffer back.

## Resume Prompt (maintenance / v0.2)
> You are a Senior Python Data Engineer maintaining the India Startup Internship Scraper (v0.1 complete). Read
> `context.md`, `implementation.md`, `AGENTS.md`, `docs/ARCHITECTURE.md`, ADRs 001–005 and `PROGRESS.md` first, then
> run `uv run ruff check . && uv run ruff format --check . && uv run pytest` (expect 458 passed). Pick one item from
> "Known follow-ups", work in small green sub-steps, update `PROGRESS.md` after each, and stop for review.

---

## Phase 4 — Hidden Opportunities, Directories, Contacts, Firecrawl — ✅ COMPLETE (user approved 2026-11-26)
Funding news (Inc42 `/buzz/feed/` + `/startups/feed/`, Entrackr `/rss`), T-Hub (Lab 32 + JS-only directory via
optional Firecrawl), HN hiring (seeker posts skipped), `StartupLead`/`leads`, site discovery (ATS embeds, cold pitch on
hiring pages, GitHub org), GitHub signals, scorer, 3-pass contacts (company-domain only, MX-only), enrichment runner.
425 tests. See ADR 004.

## Phase 3 — Tier 1 Public ATS Collectors (India-filtered) — ✅ COMPLETE (user approved 2026-11-26)
Greenhouse/Lever/Ashby/Workable on `AtsCollector` (internship filter, geo with generic-only HQ fallback, India
filter, INR stipend/duration/PPO), `CollectedOpportunity`, pipeline, 16 verified seeds. 335 tests. See ADR 003.

## Phase 2 — Core HTTP, Rate Limiting & BaseCollector — ✅ COMPLETE (user approved 2026-11-26)
RFC 9309 robots parser + cached checker, polite `HttpClient`, `BaseCollector`, structured logging. 227 tests. ADR 002.

## Phase 1 — Foundation & Geo Engine — ✅ COMPLETE (user approved 2026-11-26)
uv project, docs, `config/*.yaml`, Pydantic models, SQLite repository with idempotent upserts, 3-step geo resolver.
142 tests. See ADR 001.
