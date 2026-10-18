# ATLAS — Global Financial Intelligence Platform (MVP)

A financial data platform with an AI analyst on top. ATLAS continuously ingests market and macro data, stores it with full lineage and point-in-time vintages, and answers research questions with deterministic calculations, cited evidence and verified reports.

**Design rule:** the LLM chooses tools and writes the narrative. It never computes numbers. Every figure in a report must trace back to a stored observation or a quant-engine calculation, and a verification step checks this before the report is published.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/atlas ingest          # create DB, load registry, pull all datasets from FRED (no API key needed)
.venv/bin/atlas serve           # API on http://localhost:8010  (docs at /docs)
npm --prefix apps/web install && npm --prefix apps/web run dev   # UI on http://localhost:3010
```

Run a question from the terminal:

```bash
.venv/bin/atlas research "What happens to copper, gold, and emerging-market currencies if the Fed cuts rates?"
```

To use Claude for orchestration, set `ANTHROPIC_API_KEY` (or run `ant auth login`). Without credentials, ATLAS uses its deterministic planner and writer. The deterministic writer restates the evidence but doesn't interpret it.

## Accounts

The API requires sign-in by default (`ATLAS_AUTH=required`). Research runs, reports, alerts and schedules belong to a workspace and are invisible to other workspaces; market and macro data are shared.

- **Web:** create an account on the sign-in page (`ATLAS_ALLOW_SIGNUP=true`), or create one from the CLI.
- **Existing local data** lives in the `local` workspace. To own it: `atlas create-user you@example.com --workspace local`.
- **API keys:** create on the Account page; send `Authorization: Bearer atk_…`. Keys are shown once and stored hashed.
- **Plans and quotas:** explorer / investor / research_pro / pro limits on daily research runs (and Claude runs), schedules, alerts and keys; over-limit requests get HTTP 429.
- **Passwords:** "Forgot password?" sends a single-use reset link valid for 1 hour (SMTP via `ATLAS_SMTP_*`; without SMTP the link is written to the API log for the operator). Reset requests never reveal whether an account exists. Resetting or changing a password signs the user out everywhere. Set `ATLAS_PUBLIC_URL` so links point at your web app.
- **Single-user mode:** `ATLAS_AUTH=off` skips sign-in and uses the `local` workspace.

## Deployment

`infrastructure/docker/docker-compose.yml` runs TimescaleDB, the API, the ingestion/report scheduler and the web app; the file header lists the bootstrap commands. `infrastructure/docker/smoke-test.sh` starts the stack and exercises it end to end (create user, sign in, ingest real data, run research through the API, download HTML/Excel reports, check metrics and the web app); CI runs it on every push.

## What's in the MVP

| Spec area | Implementation |
|---|---|
| Canonical registry (§5, §7.4) | `src/atlas/instruments/registry.json`: 27 instruments, 6 U.S. macro series, 13 CFTC positioning series, 208 country series (26 countries × 8 WDI indicators); NL resolution of instruments and countries |
| Data sources | FRED (prices, FX, rates, U.S. macro), CFTC Commitments of Traders (weekly positioning), World Bank WDI (annual country data), UN Comtrade (bilateral trade, 12 pre-loaded pairs incl. Bolivia–US). None needs an API key |
| Ingestion pipeline (§7) | `src/atlas/ingestion/`: raw payload storage, validation, normalization, idempotent upserts, incremental watermarks, retries/backoff, replay, refresh scheduler |
| Point-in-time macro (§7.3) | Revised values are stored as new vintages; `get_series(as_of=...)` reproduces past views |
| Freshness (§5) | Observation date, ingestion time, last check and a status (`current/delayed/stale/no_source/failing`) for every dataset |
| Quant engine (§11) | `src/atlas/analytics/`: returns, vol/EWMA, drawdowns, correlation (CI, rolling, lead/lag), HAC regression, robust-z anomalies, seasonality, walk-forward forecasts vs naive baselines, historical-beta scenarios |
| Research engine (§8) | Planner → typed tools → evidence (E1…En) → narrative → number/citation verification → one revision pass. Claude Opus 5.5 via the Anthropic SDK with server-side refusal fallback; deterministic fallback |
| Reports (§12) | Versioned HTML (print stylesheet → PDF) and Excel (summary, evidence, sources, chart data) |
| Monitoring (FR-009) | Alert rules (threshold, % move, anomaly), evaluated after each ingestion |
| Monitoring agents (FR-017, §21 Layer 3) | Saved agents watch instruments for unusual moves, large % moves, crowded CFTC positioning or high-volatility regimes; checked after each data refresh; when one fires (outside a cooldown) it runs a research template and records the evidence, with optional email; Intelligence › Agents |
| Report templates (§12) | Daily Market Brief, Commodity Deep Dive, Global Macro Outlook, Country Intelligence, Cross-Asset Research |
| Public API (FR-018) | OpenAPI docs at `/docs` with Bearer API-key auth; quickstart on the Account page |
| Scheduled reports (FR-010) | Saved questions re-run daily / weekly / monthly by the scheduler, after the data refresh |
| Trade flows (FR-016) | Annual exports/imports/balance and HS-chapter product mix per pair; `get_trade_flows` tool and Global Economy card |
| Portfolio risk (FR-015) | Saved long/short portfolios per workspace: volatility, historical/parametric VaR and CVaR, risk contributions, correlation, factor betas (dollar, 10Y yield, Brent), historical stress tests (2008, COVID, 2022); `analyze_portfolio` tool and Portfolio page |
| Event studies (§11) | Average cumulative abnormal moves around Fed decisions (inferred from the effective fed funds rate since 2009) or custom dates; `run_event_study` tool, added automatically to rate-cut/hike scenario questions, and in Quant Lab |
| Volatility regimes (§11) | Realized-vol percentile buckets plus a 2-state Markov-switching model (`detect_regime`), in research and Quant Lab |
| Positioning & countries (FR-011, §9) | COT index, z-score and crowding flags per market; cross-country comparisons with stale-value flags; country profiles |
| API (§14) | FastAPI: instruments, quotes, history, macro (with `as_of`), data/pipeline status, research jobs (202 + polling), quant, reports, alerts |
| UI (§13) | Next.js: Overview, AI Research, Markets, Global Economy, Quant Lab, Reports, Intelligence, Data Explorer |

## Honest limitations

- **Data is public and delayed.**
  - Commodity prices are IMF monthly-average spot benchmarks via FRED, not ICE/CME futures settlements.
  - FX rates are Fed H.10 reference rates.
  - Nothing here is a tradable quote, and the UI and reports say so.
- **Coverage gaps are disclosed, not filled.**
  - Gold and silver have no free redistributable price source, so their prices show as `no_source`. Their CFTC positioning is available.
  - World Bank data is annual and lagged; values far older than peers are flagged and excluded from rankings.
  - Fundamentals, futures curves and documents are tools that return "not connected". The gap is recorded as evidence.
- **Stack differences from the spec:**
  - SQLite by default; Postgres/Timescale via `ATLAS_DATABASE_URL`.
  - A thread pool instead of Celery/Redis, and a polling scheduler instead of Dagster.
  - Plain CSS instead of Tailwind.
  - All interfaces are modular so these can be swapped.
- **Not built yet:**
  - billing and plan upgrades (plans are set by an operator);
  - email verification at sign-up;
  - higher-frequency non-US data (IMF/BIS connectors);
  - USDA/EIA connectors (need free API keys);
  - licensed market data;
  - native PDF rendering.
- **Claude orchestration** is verified live: research runs on Claude Opus 5.5 pass number and citation verification. `atlas llm-check` confirms credentials.

## Layout

```
src/atlas/
  instruments/   canonical registry + resolution
  provider_sdk/  adapter interface, retries
  ingestion/     FRED connector, quality checks, pipeline, scheduler
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

See `.env.example`. Key variables: `ATLAS_DATABASE_URL`, `ATLAS_OBJECT_STORE`, `ATLAS_LLM` (`auto`/`off`), `ATLAS_LLM_MODEL` (default `claude-opus-5-5`), `ATLAS_LLM_EFFORT`, `ATLAS_SCHEDULER=on` (refresh inside the API process), `NEXT_PUBLIC_API_URL` for the web app.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on pull requests: the Python tests on 3.12 and 3.13 (SQLite) and again on TimescaleDB/PostgreSQL 16, a web type-check and production build, and a build of both Docker images with an API smoke test. To run the suite against Postgres locally, set `ATLAS_TEST_DATABASE_URL` (the database is wiped).

## Monitoring

`GET /metrics` serves Prometheus text format: HTTP request counts and latency histograms per route template, research runs by status and engine, 30-day pipeline runs by status, datasets by freshness, and LLM configuration. Set `ATLAS_METRICS_TOKEN` to require a bearer token. Outbound requests identify as `ATLAS_USER_AGENT` (default `ATLAS-research/0.1 (+contact URL)`); set your own contact URL in production.

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
