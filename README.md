# India Startup Internship & Hidden-Opportunity Scraper

A CLI-first, zero-infrastructure pipeline that finds **fresh startup internships in India** and
**hidden opportunities** (recently funded startups, incubator cohorts, active GitHub orgs) with
**city-level filtering** — Hyderabad, Bengaluru, Chennai, Mumbai, Delhi NCR, Pune, Kolkata,
Ahmedabad — plus **Remote India**, and **ethical public contact discovery**.

> **Status:** v0.1 feature-complete (Phases 1–5). ATS internships (Greenhouse, Lever, Ashby, Workable),
> funding news (Inc42, Entrackr), T-Hub directories, HN hiring thread, careers-page discovery,
> GitHub signals, 0–100 hidden-opportunity score, 3-pass public contacts (DNS MX only), CSV/JSON
> exports, a daily GitHub Actions run, and an optional local NiceGUI dashboard (Phase 6).
> See [`PROGRESS.md`](PROGRESS.md).

## 5-minute setup

Prerequisites: [`uv`](https://docs.astral.sh/uv/) (it installs Python 3.12 for you).

```bash
git clone <your-fork-url> && cd india-internship-scraper
uv sync                       # creates .venv and installs all deps from uv.lock
cp .env.example .env          # optional; defaults work out of the box
uv run pytest                 # offline test suite — should be all green
uv run ruff check .           # lint
uv run scraper version        # CLI smoke test
```

## CLI usage

```bash
uv run scraper scrape                     # all collectors -> data/scraper.db
uv run scraper enrich --limit 25          # careers pages, GitHub, contacts, hidden score
uv run scraper export --cities hyderabad --include-remote --format csv
uv run scraper stats
```

Typical daily loop: **scrape → enrich → export**. Everything is idempotent: re-running never
duplicates rows, and postings that disappear from a board are marked `closed`.

### Global options (before the command)

| Option | Default | Meaning |
|---|---|---|
| `--db PATH` | `SCRAPER_DB_PATH` (`data/scraper.db`) | SQLite file |
| `--config-dir DIR` | `SCRAPER_CONFIG_DIR` (`config`) | YAML configs |
| `--log-level LEVEL` | `SCRAPER_LOG_LEVEL` (`INFO`) | logging verbosity |

### `scrape`

| Option | Meaning |
|---|---|
| `--sources a,b` / `-s` | Collectors to run (default all): `greenhouse`, `lever`, `ashby`, `workable`, `rippling`, `breezyhr`, `recruitee`, `zohorecruit`, `funding_news`, `thub`, `hn_hiring`, `yc_india`, `iitm`, `nsrcel`, `himalayas`, `remotive`, `internshala`, `cutshort`, `instahyre`; aliases `inc42`, `entrackr` pick one funding feed. |
| `--cities a,b` / `--city` | Targeted pass: only ATS seeds whose `hq_city` is listed. |
| `--dry-run` | Print every collector and target; no network, no database writes. |

Boards discovered on careers pages during `enrich` are polled automatically on the next `scrape`.

**India sources (v0.2, ADR 008).**
- `zohorecruit` polls Zoho Recruit careers portals. The seed `slug` is the full portal host,
  e.g. `acme.zohorecruit.in`.
- `yc_india` reads YC's India directory. It records batch, team size, GitHub org and whether the
  company is on YC's "hiring" list.
- `iitm` (IIT Madras Incubation Cell) and `nsrcel` (IIM Bangalore) read incubator portfolios.
  Their `default_city` (Chennai or Bengaluru) applies only when a row has no city.
- All of these respect robots.txt.
- Keka portals are detected but not scraped, because their job data path is disallowed by
  robots.txt.

**More public ATS APIs (Batch A, ADR 009).**
- `rippling`, `breezyhr` and `recruitee` poll those providers' public job-board JSON, with
  robots.txt respected.
- They also join `enrich` slug probing and `scraper discover --provider rippling|breezyhr|recruitee`.
- SmartRecruiters is detection only: its API's robots.txt disallows all crawlers except
  LinkedInBot.

**Remote internships open to India (Batch B, ADR 010).** Both feeds respect robots.txt.
- `himalayas` reads Himalayas' public search API: India interns plus worldwide interns, page 1
  only, because `?page=` is robots-disallowed.
- `remotive` reads Remotive's RSS feed. Its JSON API is robots-disallowed and is not used.
- A role is kept only if it is restricted to India, is IST-only, or is open worldwide. The rules
  live in `sources.yaml: remote_feeds`.

**Indian student portals (Batch C, ADR 011).** `internshala`, `cutshort` and `instahyre` read
public listing pages by city: Bengaluru, Hyderabad, Chennai and remote. `--cities` narrows the
city pages.
- **Terms of Service:** all three portals' Terms prohibit automated access. The project owner chose
  to collect their public listings anyway; ADR 011 records the risk.
- **Politeness:** robots.txt is respected, requests are paced per site, and nothing bypasses a
  login, CAPTCHA or firewall.
- **To opt out:** remove a portal's block from `sources.yaml: student_portals`.
- **Non-targets:** Unstop blocks crawlers with a 403, and Cuvette's domain does not resolve.
Exit code 1 only if *every* target failed.

### `enrich`

| Option | Meaning |
|---|---|
| `--limit N` | Enrich at most N startups (never-scored first, then least recently checked). |
| `--cities a,b` | Target cities for the scorer's location signal (default: any Indian city). |

Uses DNS **MX lookups only** for email patterns, `GITHUB_TOKEN` if set, and Firecrawl **only** when
`FIRECRAWL_API_KEY` is set. For a startup with no known ATS and no board embedded on its careers
pages, `enrich` also **probes** a few conservative slugs (`acme.in` → `acme`) on the Greenhouse /
Lever / Ashby / Workable public APIs and stores a board only if it provably belongs to the startup
(its postings link the startup's domain, or its name matches exactly) — ADR 007.

### `discover` (automatic ATS board discovery)

```bash
uv run scraper discover --dry-run                       # show the search queries; no network
uv run --env-file .env scraper discover --cities hyderabad,bengaluru --limit 20
uv run scraper discover --provider lever
```

| Option | Default | Meaning |
|---|---|---|
| `--provider` | `all` | `all`, `greenhouse`, `lever`, `ashby`, `workable` |
| `--cities a,b` | `ats_search.default_cities` | city slugs used in the query templates (+ an "India" query) |
| `--limit N` | `20` | verify at most N new boards per run |
| `--dry-run` | off | print backend + queries, no network |

Needs `BRAVE_SEARCH_API_KEY` (Brave Search API) or `FIRECRAWL_API_KEY` (Firecrawl `/search`) in `.env`;
without either it prints a setup hint and makes **no** network calls. Only official search APIs are
used (never search-engine HTML). Each board found is verified on the live ATS JSON API, its company
domain is inferred from the postings, and it is saved in SQLite (`startups`, `discovered_via="ats_search"`)
so the next `scraper scrape` polls it. `config/sources.yaml` is never modified. Boards whose domain
can't be inferred are saved as leads for review; a board that conflicts with a startup's existing
board is reported, not overwritten. Also available as **Discover boards** in the dashboard.

### `export`

| Option | Default | Meaning |
|---|---|---|
| `--what` | `opportunities` | `opportunities`, `startups`, `leads`, `contacts` |
| `--cities a,b` | all | canonical slugs: `hyderabad`, `bengaluru`, `chennai`, `mumbai`, `delhi_ncr`, `pune`, `kolkata`, `ahmedabad` |
| `--include-remote` | off | also include Remote-India roles (`(city AND mode) OR remote_india`) |
| `--work-mode a,b` | all | `onsite`, `hybrid`, `remote_india`, `remote_global` |
| `--min-hidden-score N` | none | startup score ≥ N (0–100); unscored / startup-less rows drop out |
| `--status` | `open` | `open`, `closed`, `all` (opportunities) |
| `--format` | `csv` | `csv` or `json` |
| `--out PATH` | `exports/<what>_<YYYYMMDD>.<fmt>` | file path, or `-` for stdout |

```bash
# Hyderabad internships + India-remote roles
uv run scraper export --cities hyderabad --include-remote --format csv

# South India, on-site/hybrid only, high hidden-opportunity score
uv run scraper export --cities hyderabad,bengaluru,chennai --work-mode onsite,hybrid --min-hidden-score 60

# Startups worth a cold pitch, and their public recruiting contacts
uv run scraper export --what startups --min-hidden-score 60
uv run scraper export --what contacts --min-hidden-score 60 --out exports/contacts.csv

# Targeted discovery pass for Hyderabad
uv run scraper scrape --sources thub,greenhouse,ashby,workable --city hyderabad
```

CSV cells starting with `= + - @` are prefixed with `'` (scraped text is never run as a spreadsheet
formula). Contacts are for **human-reviewed** outreach only.

### `stats`

`uv run scraper stats [--json]` — row counts, open roles by city / work mode / source, startups by
hidden-score band, contacts by discovery method, leads with/without a website.

### Daily automation

`.github/workflows/daily_scrape.yml` runs at 06:50 IST (and on demand): locked install, ruff +
pytest, `scrape` → `enrich` → `export`, uploads `exports/` as an artifact (14 days) and carries
`data/` (DB + ETag cache) between runs via the Actions cache. Configure in the repo settings:
`SCRAPER_USER_AGENT` (variable), optional secrets `SCRAPER_GITHUB_TOKEN`, `FIRECRAWL_API_KEY`, and
`EXPORT_CONTACTS=true` only if the repository is private.

Seeds live in `config/sources.yaml` (`ats.<provider>.companies`). Add a company only after checking
its board on the public API, and record `verified_on`.

### Local dashboard (optional, NiceGUI)

```bash
uv sync --extra ui          # one-time: installs the optional UI extra (CI never installs it)
uv run scraper ui           # http://127.0.0.1:8080, opens a browser tab
uv run scraper --db data/scraper.db ui --port 8090 --no-open
uv run --extra ui scraper ui   # one-shot alternative without a separate sync
```

| Option | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | bind address; `0.0.0.0` / `::` are refused, any other non-loopback host prints a warning |
| `--port` | `8080` | HTTP port |
| `--open/--no-open` | `--open` | open a browser tab |

What you get: KPI cards (startups, open opportunities, hidden leads, public contacts); a filter
drawer (cities, Include Remote India, work mode, min hidden score, paid only, PPO only, keyword);
four tabs (**Internships**, **Hidden Opportunities**, **Contacts** with method / MX filters and
"Unconfirmed guess" badges for 0.50 pattern emails, **Pipeline & Exports** with CSV/JSON download of
the filtered table and manual *Run scrape* / *Run enrich* buttons with a live log); Auto/Light/Dark
theme. Opening the page reads only your local SQLite file — nothing touches the network unless you
click a run button. Note: an exact `uv sync` (without `--extra ui`) removes the extra again.

## Using the geo engine today

```python
from src.enrichment.geo import GeoResolver

geo = GeoResolver.from_config()  # loads config/india_geo.yaml
loc = geo.resolve("Gachibowli, Telangana (Hybrid)")
loc.city_canonical, loc.state, loc.work_mode.value  # ('hyderabad', 'Telangana', 'hybrid')

geo.resolve("Remote - India").work_mode  # 'remote_india'
geo.resolve("India", hq_text="... Bengaluru 560 034").city_canonical  # 'bengaluru' (PIN fallback)
```

## Project layout

```
config/            india_geo.yaml · sources.yaml · scoring.yaml   (edit these, not Python)
src/core/          config, URL/text helpers, logging, polite HttpClient, robots.txt, BaseCollector
src/models/        Pydantic v2 contracts: Startup, Opportunity, Contact, ResolvedLocation
src/collectors/    ATS: greenhouse · lever · ashby · workable · rippling · breezyhr · recruitee · zohorecruit; discovery: funding_news · thub · hn_hiring · yc_india · iitm · nsrcel; remote: himalayas · remotive; portals: internshala · cutshort · instahyre
src/enrichment/    geo · internship · stipend · funding · site · ats_discovery · github_signals · scorer · contacts
src/storage/       SQLite repository with idempotent upserts
src/pipeline.py    scrape / enrich orchestration (the only place collectors meet storage)
src/exports.py     export rows, CSV/JSON rendering, stats summary
src/cli.py         Typer CLI (parsing + output only)
src/ui/            optional NiceGUI dashboard: app.py (layout) · view_models.py (pure) · jobs.py
.github/workflows/ daily_scrape.yml
tests/             offline pytest suite; fixtures in tests/fixtures/
docs/              ARCHITECTURE.md, ADRs/
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `SCRAPER_DB_PATH` | `data/scraper.db` | SQLite file (parent dir auto-created) |
| `SCRAPER_CONFIG_DIR` | `config` | Directory with the three YAML files |
| `SCRAPER_USER_AGENT` | see `.env.example` | Transparent UA for all requests (put a contact URL in it) |
| `SCRAPER_HTTP_CACHE_DIR` | `data/http_cache` | ETag / Last-Modified cache (safe to delete) |
| `GITHUB_TOKEN` | empty | Optional, raises GitHub API rate limit (Phase 4) |
| `FIRECRAWL_API_KEY` | empty | Optional Tier 3 JS-render fallback (Phase 4) and `discover` search backend |
| `BRAVE_SEARCH_API_KEY` | empty | Optional Brave Search API key for `scraper discover` (preferred backend) |
| `SCRAPER_LOG_LEVEL` | `INFO` | Logging verbosity |
| `SCRAPER_LOG_FORMAT` | `text` | `text` (key=value) or `json` (one object per line) |

Load a `.env` with `uv run --env-file .env scraper <command>`.

**Add a city:** append a block under `cities:` in `config/india_geo.yaml` (aliases, neighborhoods,
PIN prefixes, centroid). No code change needed; `uv run pytest` validates there are no alias collisions.

## Ethics & compliance

Public data only. No login-walled networks, no CAPTCHA bypass, `robots.txt` respected (RFC 9309),
one request at a time per site with 1.5–2.5 s gaps, at most 5 requests in flight, and email checks
via DNS MX only (never SMTP probing). Outreach stays human-reviewed. Full rules: [`AGENTS.md`](AGENTS.md).

## Further reading
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — pipeline and how to add a collector
- [`docs/ADRs/001-stack-and-storage.md`](docs/ADRs/001-stack-and-storage.md) — why this stack
- [`docs/ADRs/002-http-politeness-and-collector-contract.md`](docs/ADRs/002-http-politeness-and-collector-contract.md) — rate limits, robots.txt, collector interface
- [`docs/ADRs/003-ats-collectors-and-india-filtering.md`](docs/ADRs/003-ats-collectors-and-india-filtering.md) — ATS normalization, HQ fallback, stipend parsing, seeds
- [`docs/ADRs/004-leads-discovery-contacts.md`](docs/ADRs/004-leads-discovery-contacts.md) — leads table, discovery sources, scoring, contact guardrails
- [`docs/ADRs/005-cli-exports-automation.md`](docs/ADRs/005-cli-exports-automation.md) — CLI semantics, closing postings, exports, daily workflow
- [`docs/ADRs/006-local-nicegui-ui.md`](docs/ADRs/006-local-nicegui-ui.md) — optional local dashboard, privacy/bind rules, UI layering
- [`docs/ADRs/007-automatic-ats-seed-discovery.md`](docs/ADRs/007-automatic-ats-seed-discovery.md) — slug probing, anti-collision, `scraper discover`
- [`docs/ADRs/011-indian-student-portals.md`](docs/ADRs/011-indian-student-portals.md) — compliance audit, owner decision on Terms, Unstop/Cuvette non-targets
- [`docs/ADRs/010-remote-job-feeds.md`](docs/ADRs/010-remote-job-feeds.md) — Himalayas + Remotive RSS, India-openness rule, Remotive API as a non-target
- [`docs/ADRs/009-expanded-ats-providers.md`](docs/ADRs/009-expanded-ats-providers.md) — Rippling, BreezyHR, Recruitee; SmartRecruiters as a non-target
- [`docs/ADRs/008-india-ats-and-directories.md`](docs/ADRs/008-india-ats-and-directories.md) — Zoho Recruit, YC India, IITM, NSRCEL; why Keka is skipped; additive lead migration
- [`context.md`](context.md) · [`implementation.md`](implementation.md) — product & technical spec
