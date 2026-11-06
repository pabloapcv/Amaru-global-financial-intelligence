# Amaru — Global Financial Intelligence Platform

**The problem:** Macro and commodity research is buried in LLM hallucination, estimation, and disconnected data. When you ask "What happens to copper if the Fed cuts rates?", you need numbers traced to real observations, not a model guessing.

**The solution:** Amaru ingests market and macro data with full lineage and point-in-time vintages, answers research questions with a verified pipeline (every figure traces to a stored observation or quant-engine calculation), and surfaces honest limitations where data isn't connected.

> **Naming note:** Amaru was previously called ATLAS. Internal names keep the old name for compatibility: the `atlas` Python package/CLI, `ATLAS_*` settings, `X-Atlas-*` headers, and `atk_` API keys. A full rename is not in scope for this release.

---

## Quant Capabilities

Amaru's quant engine (`src/atlas/analytics/`) runs deterministic, reproducible calculations:

| Capability | Method | Example |
|------------|--------|---------|
| **HAC Regressions** | Newey-West robust standard errors | Fed rate → copper sensitivity with 95% CI |
| **Regime Detection** | 2-state Markov-switching (Hamilton filter) | High-vol regime probability now |
| **Walk-Forward Forecasts** | Rolling-origin evaluation vs naive baselines | Does the model beat no-change? |
| **VaR / CVaR** | Historical and parametric, with risk contributions | Portfolio 95% VaR, top risk contributors |
| **Event Studies** | Mean-adjusted abnormal returns around events | Gold reaction to Fed cuts |
| **Price vs Fundamentals** | USDA stocks-to-use regression | Implied corn price from the projection |

**See the artifact:** [`docs/research/corn_supply_demand_report.md`](docs/research/corn_supply_demand_report.md) — a verified research report showing the stocks-to-use model with walk-forward validation. Regenerate it with `python demos/research/corn_supply_demand.py`.

---

## Quick Start (5 minutes)

```bash
# 1. Install
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 2. Ingest data (FRED public data, no API key needed)
.venv/bin/atlas ingest

# 3. Run a research question
.venv/bin/atlas research "What happens to copper if the Fed cuts rates 50bp?"
```

**Demo without API keys:**

```bash
make demo   # Uses synthetic fixture data, no Anthropic key needed
```

The demo runs the corn supply-demand artifact and outputs `docs/research/corn_supply_demand_report.md`.

---

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Data Sources  │───▶│  Ingestion +     │───▶│  Quant Engine   │
│  FRED, USDA,    │    │  Point-in-Time   │    │  HAC, HMM, VaR  │
│  CFTC, BIS...   │    │  Vintage Store   │    │  Walk-Forward   │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                       │
                              ┌─────────────────────────┘
                              ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Verification  │◀───│  Research Engine │───▶│   Reports       │
│  Every number   │    │  Claude or       │    │  HTML, PDF,     │
│  traces to data │    │  deterministic   │    │  Excel          │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

**Design rule:** The LLM chooses tools and writes narrative. It never computes numbers. Every figure must trace back to a stored observation or quant-engine calculation, and verification checks this before publishing.

---

## Data Sources (no API keys required)

| Source | Coverage | Frequency |
|--------|----------|-----------|
| FRED | Commodity prices (IMF), FX, rates, US macro | Daily/Monthly |
| CFTC | Speculator/hedger positioning | Weekly |
| World Bank | 26 countries × 8 indicators, Pink Sheet commodities | Annual/Monthly |
| USDA PSD | Supply-demand: corn, wheat, soybeans, sugar, coffee, cotton, rice | Monthly |
| BIS | Central bank policy rates (19 banks), credit gaps, REER | Daily/Quarterly |
| EIA | US energy inventories (requires free key for full access) | Weekly |
| UN Comtrade | Bilateral trade flows | Annual |

---

## Running with Claude (Optional)

Set `ANTHROPIC_API_KEY` for LLM-orchestrated research:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/atlas research "What happens to emerging-market currencies if oil rises 20%?"
```

Without credentials, Amaru uses its deterministic planner and writer. The deterministic engine restates evidence but doesn't interpret it.

---

## API and Web UI

```bash
.venv/bin/atlas serve                              # API on http://localhost:8010 (docs at /docs)
npm --prefix apps/web install && npm --prefix apps/web run dev   # UI on http://localhost:3010
```

Docker (TimescaleDB + API + scheduler + web):

```bash
cp .env.example .env
docker compose -f infrastructure/docker/docker-compose.yml --env-file .env up -d --build
docker compose exec api atlas ingest
```

---

## Project Layout

```
src/atlas/
  analytics/       Quant engine (HAC, HMM, VaR, walk-forward, event studies)
  ingestion/       Provider adapters (FRED, CFTC, USDA, BIS, EIA...)
  research/        Planner, tools, Claude loop, deterministic writer, verification
  reporting/       HTML/Excel/PDF rendering
  instruments/     Canonical registry + NL resolution
  data/            Schema + unified data access layer
  api/             FastAPI app
demos/
  research/        Runnable research artifacts (regenerable)
apps/web/          Next.js frontend
tests/             Unit, integration, financial validation
docs/
  research/        Sample research outputs (committed)
  monetization.md  Business considerations
  deploy.md        Production deployment guide
```

---

## Honest Limitations

- **Prices are public benchmarks, not tradable quotes.** Monthly IMF averages via FRED, not exchange settlements.
- **Coverage gaps are disclosed, not filled.** Metals fundamentals, futures curves, news wires → return "not connected."
- **The LLM can be wrong.** Verification catches fabricated numbers, but interpretation is the model's opinion.
- **Walk-forward tests are honest.** If the model doesn't beat naive baselines, the report says so.

---

## Further Documentation

- **[Monetization & Business](docs/monetization.md)** — Data licensing, regulation, pricing considerations
- **[Production Deployment](docs/deploy.md)** — HTTPS, backups, monitoring
- **[Security](SECURITY.md)** — Vulnerability reporting, security features

---

## Tests

```bash
.venv/bin/python -m pytest
```

Tests run against an isolated database with synthetic data (no network). They cover ingestion idempotency, financial calculations, verification of fabricated numbers, and end-to-end research.

---

*Research and decision support only. Not investment advice.*
