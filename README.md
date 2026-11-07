# Amaru — open financial research platform

A personal research and portfolio project for a master's in data science. Amaru is not a product, not a hosted service, and it does not charge anyone.

It explores a practical question: can a research assistant over public market and macro data stay honest about every number it prints? The platform continuously ingests official series, stores them with lineage and point-in-time vintages, and answers questions with deterministic calculations, cited evidence and a verification step. The main market in the registry is the United States (Fed, Treasury yields, the dollar, inflation, jobs, energy, farm and commodity markets), with 25 other economies (G20 and Latin America) for context.

Amaru was called ATLAS. Internal names keep the old name so existing setups keep working: the `atlas` Python package and command, `ATLAS_*` settings, the `X-Atlas-*` webhook headers, the `atk_` API key prefix and the backup file format.

**Design rule:** the language model chooses tools and writes the narrative. It never computes numbers. Every figure in a report must trace back to a stored observation or a quant-engine calculation, and a verification step checks this before the report is shown.

## What it explores

- **Traceable AI research.** Claude (when configured) plans which tools to call and drafts the write-up. Numbers come only from stored series or from `src/atlas/analytics`. Fabricated or untraceable figures are flagged, not hidden.
- **Point-in-time macro.** Revised official values are stored as new vintages, so `get_series(as_of=…)` can reproduce what was knowable on a past date rather than silently using today's revised print.
- **Scenario methodology.** A Scenario Agent scans stored data for rate, dollar, oil, volatility, positioning and supply shocks, estimates historical sensitivities, and walk-forward tests them. It does not produce probabilities or trading signals.
- **Public, delayed data.** IMF/FRED commodity averages, Fed H.10 FX, USDA balances, CFTC positioning, EIA inventories, World Bank, BIS, IMF and UN Comtrade. No licensed futures feed is connected.

## Architecture

```
ingestion adapters → raw payloads + quality checks → idempotent store (SQLite or Postgres/Timescale)
                                                          ↓
                                         data access layer (quotes, history, as_of vintages)
                                                          ↓
                              planner → typed tools → evidence [E1…En] → narrative → verification
                                                          ↓
                                         HTML / PDF / Excel reports
```

- **Ingestion** (`src/atlas/ingestion/`): FRED, CFTC, World Bank, UN Comtrade, USDA FAS PSD, BIS, EIA and related feeds. Raw payloads are kept; upserts are idempotent; watermarks drive incremental refresh.
- **Quant engine** (`src/atlas/analytics/`): returns, volatility, drawdowns, correlation, HAC regression, anomalies, seasonality, walk-forward forecasts vs naive baselines, historical-beta scenarios. The LLM does not run these itself.
- **Research engine** (`src/atlas/research/`): intent parsing and a deterministic plan; Claude Opus (Anthropic SDK) or the deterministic planner/writer; number and citation verification with one revision pass.
- **API and UI:** FastAPI (`atlas serve`) and a Next.js app (`apps/web`). Research jobs are queued in the database.

Workspaces isolate research, reports, alerts and schedules. Market and macro data are shared. Auth is on by default; `ATLAS_AUTH=off` is single-user local mode.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/atlas ingest          # create DB, load registry, pull datasets from FRED (no API key needed)
.venv/bin/atlas serve           # API on http://localhost:8010  (docs at /docs)
npm --prefix apps/web install && npm --prefix apps/web run dev   # UI on http://localhost:3010
```

Run a question from the terminal:

```bash
.venv/bin/atlas research "What happens to copper, gold, and emerging-market currencies if the Fed cuts rates?"
```

To use Claude for orchestration, set `ANTHROPIC_API_KEY` (or run `ant auth login`). Without credentials, Amaru uses its deterministic planner and writer. The deterministic writer restates the evidence but doesn't interpret it.

## Data sources and vintages

| Source | What it supplies |
|---|---|
| FRED | Prices, FX, rates, U.S. macro. Some series belong to third parties (S&P 500, NASDAQ, VIX). |
| CFTC | Weekly Commitments of Traders positioning |
| World Bank WDI | Annual country indicators |
| World Bank Pink Sheet | Monthly average commodity prices (gold, silver, fertilizers and others) |
| UN Comtrade | Bilateral trade, including U.S. pairs with China, Mexico, Canada, Japan and Germany |
| USDA FAS PSD | Agricultural supply-and-demand balances since 1990 |
| BIS | Daily central-bank policy rates; REER, credit-to-GDP gaps, house prices |
| IMF (via FRED / WEO) | Commodity averages, CPI, World Economic Outlook forecasts |
| EIA | Weekly U.S. oil and gas inventories (`EIA_API_KEY` optional; demo key otherwise) |
| Official sites | Fed, BEA, CFTC, USDA AMS, ECB, Bank of England, Banco Central do Brasil press releases |

Every sourced dataset carries a licence class (public domain, CC BY 4.0, check terms, third-party restricted), the owner and a note, shown in Data Explorer, instrument provenance and export headers. `ATLAS_COMMERCIAL_MODE=true` switches off restricted series (S&P 500, NASDAQ, VIX) so they are not ingested or served.

**Point-in-time:** revised values are stored as new vintages. `GET /api/v1/...` history endpoints and `get_series(as_of=...)` reproduce the view as of a past date. IMF WEO forecasts are kept the same way: each edition is a vintage, earlier forecasts stay visible.

Freshness is tracked per dataset: observation date, ingestion time, last check, and a status (`current` / `delayed` / `stale` / `no_source` / `failing`).

## Scenario Agent

The Scenario Agent page (or the `scenario_outlook` report template, or the `scan_scenarios` research tool) scans stored data for the scenarios it points to, using rules anyone can audit:

- an FOMC meeting in the next 60 days or a large 2-year-yield move → rates ±50 bp;
- a large 3-month dollar or oil move, or one at an extreme of its 5-year range → dollar ±5%, oil ±20%;
- a calm VIX → a volatility spike;
- CFTC speculators at an extreme of their 3-year range → a positioning unwind;
- USDA stocks-to-use at an extreme of 20 years → a supply squeeze or surplus;
- plus up to five scenarios of your own.

Each scenario lists its signals and its effect on up to 15 markets from 10 years of historical sensitivities (HAC regressions of period changes), with 95% ranges, and effects that aren't statistically significant are marked. Beside each sensitivity is what actually happened: the market's median move over the past non-overlapping 3-month windows (up to 30 years) when the driver moved at least that much, how many of those episodes went the same way, and a warning when the median contradicts the sensitivity. Fewer than 3 episodes are reported as too few, and a shock with no precedent says so.

A saved portfolio can be tested on any scan: it is measured as one monthly series at its current weights, so correlations between holdings are included, and the largest contributors are shown. Scenarios are ranked by how many signals point to them, which is not a probability, and sensitivities describe how markets moved together, not causes.

Each scenario also carries a walk-forward test: for every past episode the sensitivities are refitted on the 10 years before it began, applied to the driver's actual move and compared with what markets did, against the benchmark of assuming no move. The page and the report show how often they pointed the right way and how their errors compare with that benchmark, for a tested portfolio too (at today's weights, so it shows how the mix would have behaved, not a real track record).

The write-up goes through the usual number verification and produces a normal report. Each scan is compared with the workspace's previous one: new, stronger, weaker and dropped scenarios (counting only those with data signals) lead the write-up and are marked on the page. When a scan finds something new or stronger, a `scenarios.changed` webhook fires, and for a scheduled scan the schedule's creator gets an email.

API: `POST /api/v1/research` with `report.template = "scenario_outlook"` and optional `scenarios: {universe: [...], custom: [{driver, shock, name}], portfolio_id}`.

## How to run it

The web app's `/welcome` page is a short project landing (English, Spanish, Portuguese). Sign in at `/login`. Create an account on that page when `ATLAS_ALLOW_SIGNUP=true`, or from the CLI:

```bash
atlas create-user you@example.com --workspace local
```

Existing local data lives in the `local` workspace; that command takes ownership of it.

**API keys:** create on the Account page; send `Authorization: Bearer atk_…`. Keys are shown once and stored hashed. OpenAPI is at `/docs`.

**Rate limits and resource caps:** each workspace has a named limit set (`explorer` by default; `investor`, `research_pro`, `pro` raise the caps). These are local resource limits, not paid tiers. Over-limit requests get HTTP 429. Operators change them with `atlas set-plan <workspace_id> <plan>` or the operator console. Browser sessions get five times the API-key requests-per-minute (at least 300). Responses carry `X-RateLimit-Limit` and `X-RateLimit-Remaining`. With several API replicas set `ATLAS_RATE_LIMIT_STORE=database`.

**Claude cost control:** every Claude run records token usage and estimated cost (shown on the run and the Account page). Each limit set has a monthly Claude budget; once reached, `auto` runs fall back to the deterministic engine and explicit `llm` runs are refused. Prompt caching cuts the repeated system/tool prefix.

**Result cache:** an identical question (same settings) asked again within `ATLAS_RESEARCH_CACHE_TTL_HOURS` (default 6) with no data changed since is answered from the earlier verified run; pass `"no_cache": true` to force fresh research.

Other account features (password reset, email verification, two-factor, teams, webhooks, activity log, personal-data export and deletion, weekly digest, newsletters) work as before. They are there so the research workflow can be used, not as a hosted product. Optional `ATLAS_PRIVACY_URL` is linked from public newsletter pages.

**Operators:** list emails in `ATLAS_OPERATOR_EMAILS`. An operator signed in with two-factor sees the operator console: workspaces, members, research volume, Claude spend and pipeline success. From there they change a workspace's limit set or suspend it. Research content is never shown, except feedback readers chose to send.

**Single-user mode:** `ATLAS_AUTH=off` skips sign-in and uses the `local` workspace.

Run `atlas doctor` on any deployment (add `--production` to apply production rules). It checks sign-in, the encryption key, secure cookies, CORS and public URLs, the database, email, Claude, operators, metrics, webhooks and data-licence settings. It never prints secret values, and exits 1 on errors.

**Production:** [docs/deploy.md](docs/deploy.md) puts Amaru on one server with HTTPS (Caddy), secure cookies and daily backups. `infrastructure/docker/docker-compose.yml` runs TimescaleDB, the API, the scheduler and the web app. `infrastructure/docker/smoke-test.sh` starts the stack and exercises it end to end.

## What's implemented

| Area | Implementation |
|---|---|
| Canonical registry | `src/atlas/instruments/registry.json`: 27 instruments, 6 U.S. macro series, 13 CFTC series, 208 country series (26 × 8 WDI), 19 policy rates |
| Ingestion pipeline | Raw payload storage, validation, normalization, idempotent upserts, watermarks, retries, replay, refresh scheduler |
| Quant engine | Returns, vol/EWMA, drawdowns, correlation, HAC regression, robust-z, seasonality, walk-forward forecasts, historical-beta scenarios; USDA price-vs-stocks-to-use in Quant Lab |
| Research engine | Planner → typed tools → evidence → narrative → number/citation verification → one revision pass. Briefs in English, Spanish or Portuguese |
| Reports | Versioned HTML, native PDF (WeasyPrint) and Excel. Read-only share links (expiry, revocable, hashed token) |
| Monitoring | Alert rules; agents that run a research template when data or official releases change |
| Report templates | Daily Market Brief, Commodity Deep Dive, Global Macro Outlook, Country Intelligence, Cross-Asset Research, Scenario Outlook |
| Portfolios | Long/short books: vol, historical/parametric VaR and CVaR, risk contributions, factor betas, historical stress tests |
| Supply and demand | USDA PSD for sugar, coffee, corn, wheat, soybeans, cotton and rice; EIA weekly U.S. energy inventories |
| UI | Overview, AI Research, Markets, Global Economy, Quant Lab, Scenario Agent, Portfolio, Reports, Intelligence, Data Explorer |

See the module docstrings and OpenAPI (`/docs`) for the rest of the surface (calendar, documents, fertilizer affordability, country monitor, business-cycle probit, policy rates, central-bank tone, trade flows, watchlist, scheduled reports).

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability, what Amaru already does (hashed tokens, two-factor, workspace isolation, sandboxed report pages, rate limits, audit log) and how to operate it securely. The Security workflow audits Python and npm dependencies on every push and weekly.

## Honest limitations

- **Data is public and delayed.**
  - Commodity prices are IMF monthly-average spot benchmarks via FRED, not ICE/CME futures settlements.
  - FX rates are Fed H.10 reference rates.
  - Nothing here is a tradable quote, and the UI and reports say so.
- **Coverage gaps are disclosed, not filled.**
  - Gold, silver and the other World Bank Pink Sheet prices are monthly averages, so nothing daily is available for them.
  - World Bank data is annual and lagged; values far older than peers are flagged and excluded from rankings.
  - Supply-and-demand data covers USDA PSD agricultural commodities and EIA weekly U.S. energy inventories; metals and cocoa fundamentals, non-U.S. energy stocks and all futures curves return "not connected", recorded as evidence.
  - Document search covers official press releases only (Fed, BEA, CFTC, USDA AMS, ECB, Bank of England, and the Banco Central do Brasil in Portuguese). BLS and the main USDA site block automated feed access, the IMF's press feed no longer exists and Banxico removed its feed (rechecked November 2026). Search matches words, so English questions only find Portuguese releases through shared terms such as Copom or Selic. There is no news wire or sell-side research.
- **Licences are not legal advice.** Classification of each series (public domain, CC BY 4.0, check terms, restricted) is a best-effort reading of the source terms as of late 2026. BIS series must be cited as BIS statistics. Several IMF, Comtrade and Pink Sheet series need the owner's terms checked before redistribution.
- **Stack differences from the original spec:**
  - SQLite by default; Postgres/Timescale via `ATLAS_DATABASE_URL`.
  - A research queue in the database instead of Celery/Redis, and a polling scheduler instead of Dagster. Queued research survives restarts: workers claim runs one at a time, keep a heartbeat, and a run whose server stopped is started again once (its first attempt's Claude cost still counts) and fails if it is lost twice. Cancelling works from any API replica. `ATLAS_RESEARCH_WORKERS` (default 4) sets runs at a time per process; set it to 0 on API processes and run `atlas worker` elsewhere to keep research off the web servers.
  - Plain CSS instead of Tailwind.
- **Not built:** licensed market data.
- **Claude orchestration** is verified live: research runs on Claude Opus 5.5 pass number and citation verification. `atlas llm-check` confirms credentials.

## Layout

```
src/atlas/
  instruments/   canonical registry + resolution
  provider_sdk/  adapter interface, retries
  ingestion/     connectors, quality checks, pipeline, scheduler
  data/          schema (db.py) + unified data access layer (access.py)
  analytics/     quant engine
  research/      planner, tools, Claude loop, deterministic writer, verification, job engine
  reporting/     HTML/Excel rendering
  api/           FastAPI app
  intelligence.py  alert rules
apps/web/        Next.js frontend
tests/           unit, integration (pipeline, API, LLM loop), financial_validation
```

## Configuration

See `.env.example`. Key variables: `ATLAS_DATABASE_URL`, `ATLAS_OBJECT_STORE`, `ATLAS_LLM` (`auto`/`off`), `ATLAS_LLM_MODEL` (default `claude-opus-5-5`), `ATLAS_LLM_EFFORT`, `ATLAS_TONE_MODEL` (model that reads central-bank statements; defaults to `ATLAS_LLM_MODEL`), `ATLAS_SCHEDULER=on` (refresh inside the API process), `FRED_API_KEY` (optional; official U.S. data release dates), `NEXT_PUBLIC_API_URL` for the web app.

## Backups

```bash
atlas backup --out backups            # one .tar.gz: every table, report files, raw payloads (--no-raw to skip)
atlas restore backups/atlas-backup-20261023T153250Z.tar.gz --database-url postgresql+psycopg://...
```

Backups are database-neutral (each table as gzipped JSON lines), so a SQLite backup restores into PostgreSQL and back. Restore checks every file's SHA-256 against the manifest, rejects unsafe archive entries, and only writes into an empty database. The key that encrypts two-factor secrets (`ATLAS_SECRET_KEY` or `data/secret.key`) is never included: keep it separately, or users with two-factor sign in with a recovery code and re-enroll. In Docker: `docker compose exec api atlas backup --out /data/backups`.

**Off the server:** with `ATLAS_BACKUP_S3_URL`, its access keys and `ATLAS_BACKUP_ENCRYPTION_KEY` (make one with `atlas backup-key`) set, every backup is encrypted on the server (AES-256-GCM, in chunks that can't be reordered or cut short unnoticed) and uploaded to S3-compatible storage (AWS S3, Cloudflare R2, Backblaze B2, MinIO), then the stored size is checked. Requests are signed with AWS Signature Version 4, the access key needs no delete permission, and `atlas restore` takes the downloaded `.enc` file directly. Lose the encryption key and the copies can't be restored; see [docs/deploy.md](docs/deploy.md#backups).

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on pull requests: the Python tests on 3.12 and 3.13 (SQLite) and again on TimescaleDB/PostgreSQL 16, a web type-check and production build, and a build of both Docker images with an API smoke test. To run the suite against Postgres locally, set `ATLAS_TEST_DATABASE_URL` (the database is wiped).

## Monitoring

`GET /metrics` serves Prometheus text format: HTTP request counts and latency histograms per route template, research runs by status and engine, 30-day pipeline runs by status, datasets by freshness, and LLM configuration. Set `ATLAS_METRICS_TOKEN` to require a bearer token. Outbound requests identify as `ATLAS_USER_AGENT` (default `Amaru-research/0.1 (+contact URL)`); set your own contact URL in production.

Health: `/health` (liveness) and `/health/ready` (readiness: 200 when the database answers, 503 otherwise, plus the scheduler's lease holder and last run). Every response carries `X-Request-ID` (the caller's, if well formed); unexpected errors return 500 with that id in the body, and `%(request_id)s` is available in log formats. Only one scheduler works at a time: each iteration takes a lease row in the database (expires after 30 minutes if its holder dies), so running it in the API process and in its own container, or scaling it, never runs scheduled reports or alerts twice.

## Acceptance benchmark

`atlas benchmark` measures the spec's acceptance targets (§16) on this deployment's stored data: cached query latency, research latency, numerical traceability and citation validity across representative questions, reproducibility, 30-day pipeline reliability, freshness visibility, and cross-workspace isolation (using two throwaway accounts it removes afterwards). It exits non-zero if any target is missed; `--json` prints the full result.

## Tests

```bash
.venv/bin/python -m pytest
```

The tests run against an isolated database with synthetic data and need no network. They cover:
- idempotency, watermarks, vintages and point-in-time views, unit-change blocking, outages and replay;
- financial calculations checked by hand;
- verification of fabricated numbers;
- end-to-end research questions that must pass verification;
- the Claude tool loop.

Research and decision support only. Not investment advice.
