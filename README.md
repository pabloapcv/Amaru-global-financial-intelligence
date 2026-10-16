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

## What's in the MVP

| Spec area | Implementation |
|---|---|
| Canonical registry (§5, §7.4) | `src/atlas/instruments/registry.json`: 27 instruments + 6 macro series, NL resolution |
| Ingestion pipeline (§7) | `src/atlas/ingestion/`: raw payload storage, validation, normalization, idempotent upserts, incremental watermarks, retries/backoff, replay, refresh scheduler |
| Point-in-time macro (§7.3) | Revised values are stored as new vintages; `get_series(as_of=...)` reproduces past views |
| Freshness (§5) | Observation date, ingestion time, last check and a status (`current/delayed/stale/no_source/failing`) for every dataset |
| Quant engine (§11) | `src/atlas/analytics/`: returns, vol/EWMA, drawdowns, correlation (CI, rolling, lead/lag), HAC regression, robust-z anomalies, seasonality, walk-forward forecasts vs naive baselines, historical-beta scenarios |
| Research engine (§8) | Planner → typed tools → evidence (E1…En) → narrative → number/citation verification → one revision pass. Claude Opus 5.5 via the Anthropic SDK with server-side refusal fallback; deterministic fallback |
| Reports (§12) | Versioned HTML (print stylesheet → PDF) and Excel (summary, evidence, sources, chart data) |
| Monitoring (FR-009) | Alert rules (threshold, % move, anomaly), evaluated after each ingestion |
| API (§14) | FastAPI: instruments, quotes, history, macro (with `as_of`), data/pipeline status, research jobs (202 + polling), quant, reports, alerts |
| UI (§13) | Next.js: Overview, AI Research, Markets, Global Economy, Quant Lab, Reports, Intelligence, Data Explorer |

## Honest limitations

- **Data is public and delayed.**
  - Commodity prices are IMF monthly-average spot benchmarks via FRED, not ICE/CME futures settlements.
  - FX rates are Fed H.10 reference rates.
  - Nothing here is a tradable quote, and the UI and reports say so.
- **Coverage gaps are disclosed, not filled.**
  - Gold and silver have no free redistributable source, so they show as `no_source`.
  - Fundamentals, futures curves and documents are tools that return "not connected". The gap is recorded as evidence.
- **Stack differences from the spec:**
  - SQLite by default; Postgres/Timescale via `ATLAS_DATABASE_URL`.
  - A thread pool instead of Celery/Redis, and a polling scheduler instead of Dagster.
  - Plain CSS instead of Tailwind.
  - All interfaces are modular so these can be swapped.
- **Not built yet:**
  - authentication and multi-tenant isolation;
  - billing;
  - non-US country coverage (World Bank/IMF/BIS connectors);
  - USDA/EIA/CFTC connectors;
  - licensed market data;
  - native PDF rendering.
- **Claude orchestration is unverified live:** the tool-use loop is unit-tested against a scripted client, but it has not been run against the live API in this environment.

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
