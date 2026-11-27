# About the India Internship & Hidden-Opportunity Scraper

This tool collects **internships in India**, and remote roles open to Indian students, from public
sources. It also finds **"hidden" startups**: companies that are likely to hire interns even
without a posted internship. The data goes into a local SQLite database (`data/scraper.db`). From
there you **enrich** it (job boards, public recruiting contacts, GitHub activity, a 0–100 score),
**export** it as CSV/JSON, or browse it in a **local dashboard**.

Everything is run through one command: `scraper` (run it with `uv run scraper …`).

---

## 1. How to use it

### One-time setup

Install the dependencies:
```bash
uv sync
```

Create your settings file:
```bash
cp .env.example .env
```

Then edit `.env`. Only `SCRAPER_USER_AGENT` is required.

| Setting | Purpose |
|---|---|
| `SCRAPER_USER_AGENT` | **Required.** A transparent bot name with a contact address, sent with every request |
| `GITHUB_TOKEN` | Optional. Higher GitHub rate limits during `enrich` |
| `BRAVE_SEARCH_API_KEY` | Optional. Turns on `scraper discover` (preferred search backend) |
| `FIRECRAWL_API_KEY` | Optional. Renders JavaScript-only pages; also a search backend for `discover` |
| `SCRAPER_DB_PATH` | Database file (default `data/scraper.db`) |
| `SCRAPER_CONFIG_DIR` | Config folder (default `config/`) |
| `SCRAPER_HTTP_CACHE_DIR` | Where ETag/HTTP cache files are kept |
| `SCRAPER_LOG_LEVEL` / `SCRAPER_LOG_FORMAT` | Logging verbosity and format |

When you use API keys, run commands with the env file loaded:
```bash
uv run --env-file .env scraper stats
```

### The everyday workflow

1. **Collect** internships and startup leads from every source:
   ```bash
   uv run scraper scrape
   ```
2. **Enrich** stored startups: find their job boards, public recruiting contacts and GitHub
   activity, then score them 0–100:
   ```bash
   uv run scraper enrich --limit 25
   ```
3. **Export** what you need, for example internships in Hyderabad and Bengaluru plus Remote-India
   roles:
   ```bash
   uv run scraper export --what opportunities --cities hyderabad,bengaluru --include-remote
   ```
4. **Or browse everything** in the local dashboard, which runs on your own machine only:
   ```bash
   uv run --extra ui scraper ui
   ```

A good first step is a dry run, which lists every collector and target without touching the
network:
```bash
uv run scraper scrape --dry-run
```

---

## 2. All commands

### Global options (put these before the command)

| Option | Meaning |
|---|---|
| `--db PATH` | Use a different SQLite file (default `SCRAPER_DB_PATH`) |
| `--config-dir PATH` | Use a different config folder |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, … |

### `scrape` — collect new data

| Option | Meaning |
|---|---|
| `--sources a,b` (`-s`) | Run only some collectors; names are in §3. The default is all of them, except the `inc42` / `entrackr` aliases |
| `--cities a,b` (`--city`) | Targeted pass: only ATS seeds whose HQ is one of these cities, plus only these Internshala / Cutshort city pages. Remote pages are always kept |
| `--dry-run` | Print every collector and target. No network calls, no database writes |

The command exits with code 1 only if *every* target failed.

### `enrich` — analyse stored startups

| Option | Meaning |
|---|---|
| `--limit N` | Enrich at most N startups. Never-scored ones go first, then the least recently checked |
| `--cities a,b` | Cities that count as a good location in the score (default: any Indian city) |

`enrich` reads each startup's own careers pages. From them it:
- detects which ATS the startup uses;
- probes a few likely slugs on the public ATS APIs (a board is accepted only if it provably
  belongs to the startup);
- finds publicly listed recruiting contacts, with DNS MX checks only;
- reads GitHub org activity.

It then computes the hidden-opportunity score.

### `export` — write CSV / JSON

| Option | Meaning |
|---|---|
| `--what` | `opportunities` (default), `startups`, `leads`, `contacts` |
| `--cities a,b` (`--city`) | Filter by city |
| `--include-remote` | Also include Remote-India roles |
| `--work-mode` | `onsite`, `hybrid`, `remote_india`, `remote_global` |
| `--min-hidden-score N` | Only startups scoring ≥ N (0–100) |
| `--status` | `open` (default), `closed`, `all` |
| `--format` | `csv` (default) or `json` |
| `--out FILE` | Output file. Default `exports/<what>_<date>.<fmt>`; `-` prints to the screen |

CSV output is formula-safe: cells starting with `=`, `+`, `-` or `@` are neutralised.

### `discover` — find new public job boards via a search API

This needs `BRAVE_SEARCH_API_KEY` or `FIRECRAWL_API_KEY`. Without a key it prints a hint and
makes **no** network calls.

| Option | Meaning |
|---|---|
| `--provider` | `all`, `greenhouse`, `lever`, `ashby`, `workable`, `rippling`, `breezyhr`, `recruitee` |
| `--cities a,b` | Cities to search in (default from `sources.yaml: ats_search.default_cities`) |
| `--limit N` | Verify at most N new boards per run (default 20) |
| `--dry-run` | Print the search queries only |

Discovered boards are stored in SQLite, never in `sources.yaml`, and are polled on the next
`scrape`.

### Other commands

| Command | Meaning |
|---|---|
| `stats [--json]` | Row counts and coverage summary |
| `ui [--host 127.0.0.1] [--port 8080] [--open/--no-open]` | Local NiceGUI dashboard (needs the `ui` extra). Binds to `127.0.0.1` by default; loading a page never triggers network calls, and only its Run buttons do |
| `version` | Print the version |

**City names** accepted by `--cities`: `bengaluru`, `hyderabad`, `chennai`, `mumbai`, `delhi_ncr`,
`pune`, `kolkata`, `ahmedabad`.

---

## 3. Sources

The name in backticks is what you pass to `--sources`.

### Company job boards (ATS)

| Source | How it is read | What is kept |
|---|---|---|
| `greenhouse`, `lever`, `ashby`, `workable` | Official public job-board JSON APIs | Internships in India or Remote-India |
| `rippling`, `breezyhr`, `recruitee` | Public job-board JSON APIs, robots.txt respected | Same |
| `zohorecruit` | Zoho Recruit careers pages (`acme.zohorecruit.in/jobs/Careers`), robots.txt respected | Same |

The seed companies for each provider live in `config/sources.yaml`, and every seed was verified
live. Boards found later by `enrich` or `discover` are polled automatically.

### Startup directories and news

These produce **leads**: startups that `enrich` later checks for jobs and contacts.

| Source | What it collects |
|---|---|
| `yc_india` | Y Combinator companies headquartered in India: website, batch, city, team size, GitHub org, and whether YC lists them as hiring |
| `iitm` | IIT Madras Incubation Cell portfolio (Chennai) |
| `nsrcel` | NSRCEL / IIM Bangalore portfolio (Bengaluru): founders, sub-sector, funding stage |
| `thub` | T-Hub Hyderabad directories (Lab 32 tables; the main directory needs Firecrawl) |
| `funding_news` | Inc42 and Entrackr funding headlines ("X raises ₹…"). Use `inc42` or `entrackr` alone for one feed |
| `hn_hiring` | Hacker News "Who is hiring?": India / Remote-India / worldwide junior and intern roles. Job-seeker posts are skipped |

### Remote job feeds

| Source | What it collects |
|---|---|
| `himalayas` | Himalayas search API: internships restricted to India, IST-only, or open worldwide |
| `remotive` | Remotive's RSS feed, with the same "open to India" rule |

### Indian student portals

| Source | What it collects |
|---|---|
| `internshala` | City listings (Bengaluru, Hyderabad, Chennai) and work-from-home: title, company, location, stipend, duration |
| `cutshort` | Internships from Cutshort city and remote listings, linked to the company's own website |
| `instahyre` | Instahyre's public internship listings |

> These three portals' Terms of Service prohibit automated access. The project owner chose to
> collect their public listings anyway; the risk is recorded in `docs/ADRs/011-indian-student-portals.md`.
> To stop collecting from a portal, delete its block under `student_portals` in
> `config/sources.yaml`.

### What is extracted from every internship

- **Location:** normalised to a canonical city (Bengaluru, Hyderabad, Delhi NCR, …) or a remote
  bucket (`remote_india` / `remote_global`), with a work mode (onsite, hybrid, remote).
- **Terms:** monthly stipend in INR (min/max, or unpaid), duration in months, and whether a PPO
  (pre-placement offer) is mentioned.
- **Lifecycle:** postings that disappear from a successfully read source are marked `closed`.

---

## 4. Limitations

**Coverage**
- **Seed lists are small.** ATS coverage depends on verified seeds plus what `enrich` and
  `discover` find over time. Many Batch A seeds are India offices of global companies, with few
  internships at any moment.
- **Page caps.** Himalayas is read one page (20 jobs) per query, because robots.txt disallows its
  `?page=` links. Internshala reads 2 pages per city and Cutshort 1 page per listing. Older
  matches beyond those pages are missed.
- **Remotive rarely carries internships.** Most remote coverage comes from Himalayas.
- **Leads without a website are not enriched.** A directory or news entry with no domain stays a
  lead for manual review.

**Data quality**
- **Rippling** listings have no employment type, so an internship whose title lacks "intern" or
  "trainee" is missed.
- **Internshala** cards give only "1 week ago", and **Instahyre** has no posting date or stipend,
  so `posted_at` (and, for Instahyre, the stipend) is empty.
- **IITM and NSRCEL** list no city, so Chennai / Bengaluru is assumed when a row has no location.
- **YC's "hiring" flag** means "appears on YC's hiring list", not that an internship is open.

**Closing and accuracy**
- **Feed-style sources** (Himalayas, Remotive, the portals) close postings by absence. A
  still-open posting that falls off the first page is marked `closed` on the next successful run.
- **Board ownership is conservative.** Slug probing and `discover` accept a board only if its
  postings link the startup's domain or its name matches exactly. Some real boards are missed
  rather than mis-attributed.
- **Search backends are untested live.** `discover`'s Brave/Firecrawl parsers follow the documented
  API shapes but were tested only with mocks; verify them with a real key.

**Fragility and risk**
- **Markup dependence.** Zoho (`input#jobs`), YC (`data-page`), NSRCEL popups, Internshala cards,
  Cutshort `__NEXT_DATA__` and Instahyre's embedded JSON depend on page layout. A redesign shows
  up as a `parse` issue in the run report, not as silent data loss.
- **Sites can change their rules.** If a site changes robots.txt or starts blocking, that source
  stops with a clear `RobotsDisallowedError` / HTTP error issue. Nothing retries it another way.
- **Terms-of-Service risk** for the student portals (see §3): possible IP blocks or complaints.
- **No outreach.** The tool never sends emails or messages. Exports are for human-reviewed
  outreach.

---

## 5. Customising

Behaviour is driven by YAML, so you rarely need to touch Python. Config is validated at load time,
and a typo fails loudly.

| File | What you can change |
|---|---|
| `config/sources.yaml` | ATS seed companies (`ats:`); funding feeds; incubators (`incubators:`); YC directory (`yc_india:`); remote-feed queries and the "open to India" rules (`remote_feeds:`); student-portal city pages (`student_portals:`); internship title/type regexes (`internship:`); slug probing (`ats_probe:`); `discover` search templates (`ats_search:`); careers-page paths and cold-pitch phrases |
| `config/india_geo.yaml` | Cities, aliases ("Bangalore" → Bengaluru), neighbourhoods, PIN prefixes, remote/hybrid phrases |
| `config/scoring.yaml` | Weights and thresholds for the 0–100 hidden-opportunity score, contact confidence values |

**Common tweaks**
- **Add a company:** append `{name, slug, domain, verified_on}` under its provider in
  `sources.yaml: ats`, after checking the board loads. Zoho slugs are full hosts such as
  `acme.zohorecruit.in`.
- **Treat "APAC" remote roles as open to India:** add `apac` to
  `remote_feeds.worldwide_keywords`.
- **Drop worldwide remote roles:** set `remote_feeds.keep_worldwide: false`.
- **Add a portal city:** add a `city_slug: /path/` entry under `student_portals.internshala.targets`
  or `cutshort.targets`. The path must be a listing page with no query string.
- **Stop a source:** run without it (`--sources …`), or remove its config block.

**Daily automation.** `.github/workflows/daily_scrape.yml` runs scrape → enrich → export every day
at 06:50 IST, keeps the database and HTTP cache between runs, and uploads the CSV/JSON exports as
artifacts. Repository variables such as `ENRICH_LIMIT` tune it, and `workflow_dispatch` accepts a
`sources` input.

**Politeness built in** (do not lower these):
- robots.txt is respected; only the four documented Tier 1 ATS APIs, HN Algolia and GitHub skip
  it.
- Requests to each site are spaced 1.5–2.5 s apart, at most 5 run at once, and failures retry at
  most 3 times.
- A transparent User-Agent is sent on every request.

---

## 6. Deliberately not scraped

These are recorded as **non-targets** so nobody wastes time re-attempting them:

| Site | Reason | Where documented |
|---|---|---|
| **Keka** | Job data loads from a robots.txt-disallowed path. Keka boards are only *detected* on company careers pages | ADR 008 |
| **SmartRecruiters** | Its API's robots.txt disallows every crawler except LinkedInBot. Detection only | ADR 009 |
| **Remotive JSON API** | robots.txt disallows `/api/*`. The allowed RSS feed is used instead | ADR 010 |
| **YC's `api.ycombinator.com`** | robots.txt `Disallow: /`. The public directory pages are used instead | ADR 008 |
| **Unstop** | Its CloudFront firewall returns 403 to our crawler; getting past it would mean bypassing bot protection | ADR 011 |
| **Cuvette** | `cuvette.tech` does not resolve in DNS. Re-audit only if a new official domain is confirmed | ADR 011 |
| **LinkedIn, private X/Twitter and other login-walled sites** | Login walls are never crossed | AGENTS.md §4 |
| **Detail, apply, login and search pages** on the student portals | Disallowed by robots.txt or out of scope; only public listing pages are read | ADR 011 |
| **Job-seeker data** (e.g. "wants to be hired" posts) | Never stored | AGENTS.md §4 |
| **SMTP email probing** | Email checks are DNS MX lookups only | AGENTS.md §4 |
| **Search-engine HTML (e.g. Google results pages)** | Only official search APIs (Brave, Firecrawl) are used | ADR 007 |

Also never done: solving or bypassing CAPTCHAs, getting past Cloudflare or other bot challenges,
and retrying a robots-blocked URL another way.

---

## Further reading

- `README.md` — quick start and CLI reference
- `docs/ARCHITECTURE.md` — how the pipeline fits together and how to add a collector
- `docs/ADRs/` — every design decision (001–011)
- `PROGRESS.md` — current status and verified checks
