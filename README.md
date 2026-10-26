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
- **Rate limits:** API keys get the plan's requests per minute (60 / 120 / 300 / 600); browser sessions get five times that (at least 300), since the web app makes several calls per page. Responses carry `X-RateLimit-Limit` and `X-RateLimit-Remaining`; over the limit returns 429 with `Retry-After`. Limits are per API process; with several replicas, add a shared limiter in front.
- **Plans and quotas:** explorer / investor / research_pro / pro limits on seats (1 / 2 / 5 / 25), daily research runs (and Claude runs), schedules, alerts, agents and keys; over-limit requests get HTTP 429.
- **Billing:** with Stripe configured (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_INVESTOR` / `_RESEARCH_PRO` / `_PRO`), the workspace owner picks a plan on the Account page (Stripe Checkout) and manages payment or cancels in the Stripe Customer Portal. Plans change only from signed Stripe webhooks (`/api/v1/billing/webhook`, applied once each; a failed delivery is retried): an active subscription sets the plan from its price, a past-due one keeps it during Stripe's retries, and a cancelled one returns to Explorer. Without these settings billing stays off and operators use `atlas set-plan`.
- **Teams:** owners and admins invite colleagues by email from the Account page (owners choose admin or member). An invitation is a single-use link, stored hashed, valid 7 days, that holds a seat while open; it is emailed when SMTP is configured and shown once to the inviter. The invitee signs up through it, or signs in with the invited email and accepts; this works even when self-service signup is off. People in several workspaces switch from the navigation. Only the owner changes roles; nobody can remove or demote the owner; removing a member ends their sessions and revokes their API keys in that workspace immediately.
- **Claude cost control:** every Claude run records its token usage and estimated cost (shown on the run and the Account page). Each plan has a monthly Claude budget; once reached, `auto` runs fall back to the deterministic engine and explicit `llm` runs are refused. Prompt caching cuts the repeated system/tool prefix to ~5% of input price within a research loop.
- **Result cache:** an identical question (same settings) asked again within `ATLAS_RESEARCH_CACHE_TTL_HOURS` (default 6) with no data changed since is answered instantly from the earlier verified run, without using quota; pass `"no_cache": true` to force fresh research.
- **Passwords:** "Forgot password?" sends a single-use reset link valid for 1 hour (SMTP via `ATLAS_SMTP_*`; without SMTP the link is written to the API log for the operator). Reset requests never reveal whether an account exists. Resetting or changing a password signs the user out everywhere. Set `ATLAS_PUBLIC_URL` so links point at your web app.
- **Email verification:** sign-up sends a 48-hour verification link. With `ATLAS_REQUIRE_EMAIL_VERIFICATION=true`, unverified accounts can browse data but cannot run research or create keys, agents, schedules, portfolios or alerts. Users created with the CLI are pre-verified.
- **Two-factor authentication:** turn on from the Account page with any authenticator app (TOTP, RFC 6238); sign-in then asks for a code after the password, with 10 single-use recovery codes. Seeds are encrypted at rest (set `ATLAS_SECRET_KEY` in production), codes cannot be replayed, a sign-in challenge expires after 5 minutes or 5 attempts, and turning it on ends other password-only sessions. API keys are unaffected. Operators can reset it for a user who lost their device: `atlas disable-2fa you@example.com`. A workspace owner (who must use 2FA) can require it for everyone: members without it can then only reach their Account page to set it up, and their API keys are refused in that workspace until they do.
- **Personal data:** anyone can download their data (Account › Your data: profile, workspaces, research questions, API key metadata, portfolios, alerts, agents, schedules, activity) and delete their account with their password, a 2FA code if on, and their email typed to confirm. Deletion removes the person and every workspace where they are the only member, with all its data and report files; shared workspaces keep their content and the person's audit entries there are de-identified. Owners of shared workspaces transfer ownership first (Team › Make owner), and a workspace with a live paid subscription must cancel it first.
- **Research history:** search past questions on the Research page; the author or an owner/admin can delete a run, which removes its evidence, reports (files included) and share links and unlinks follow-ups. The run's time and Claude cost are kept so deleting cannot reset quotas or budgets; deletions are audited.
- **Weekly email:** members can opt in (Account › Weekly email, with a preview) to a Monday-morning digest for their workspace: watchlist moves, the week's scheduled events and U.S. data due, new official releases, USDA updates and the alerts and agents that fired. The scheduler sends it at most once a week; every email has a signed one-click unsubscribe link (set `ATLAS_API_PUBLIC_URL` so the link reaches your API).
- **Activity log:** owners and admins see their workspace's audit trail on the Account page (sign-ins, team changes, share links and their views, data exports, API keys, password and email events), filter it by category, page through it and download it as CSV (`GET /api/v1/activity`).
- **Operators:** `atlas workspaces` lists workspaces, plans, members and this month's Claude spend; `atlas set-plan <workspace_id> <plan>` changes a plan (audited).
- **Single-user mode:** `ATLAS_AUTH=off` skips sign-in and uses the `local` workspace.

## Deployment

`infrastructure/docker/docker-compose.yml` runs TimescaleDB, the API, the ingestion/report scheduler and the web app; the file header lists the bootstrap commands. `infrastructure/docker/smoke-test.sh` starts the stack and exercises it end to end (create user, sign in, ingest real data, run research through the API, download HTML/Excel reports, check metrics and the web app); CI runs it on every push.

## What's in the MVP

| Spec area | Implementation |
|---|---|
| Canonical registry (§5, §7.4) | `src/atlas/instruments/registry.json`: 27 instruments, 6 U.S. macro series, 13 CFTC positioning series, 208 country series (26 countries × 8 WDI indicators); NL resolution of instruments and countries |
| Data sources | FRED (prices, FX, rates, U.S. macro), CFTC Commitments of Traders (weekly positioning), World Bank WDI (annual country data), UN Comtrade (bilateral trade, 12 pre-loaded pairs incl. Bolivia–US), USDA FAS PSD (supply-and-demand balance sheets). None needs an API key |
| Ingestion pipeline (§7) | `src/atlas/ingestion/`: raw payload storage, validation, normalization, idempotent upserts, incremental watermarks, retries/backoff, replay, refresh scheduler |
| Point-in-time macro (§7.3) | Revised values are stored as new vintages; `get_series(as_of=...)` reproduces past views |
| Data licensing | Every sourced dataset carries a licence class (public domain, CC BY 4.0, check terms, third-party restricted) with the owner and a note, shown in Data Explorer, instrument provenance and export headers. `ATLAS_COMMERCIAL_MODE=true` switches off restricted series (S&P 500, NASDAQ, VIX): they are not ingested or served, including rows stored earlier, and research discloses them as gaps |
| Freshness (§5) | Observation date, ingestion time, last check and a status (`current/delayed/stale/no_source/failing`) for every dataset |
| Quant engine (§11) | `src/atlas/analytics/`: returns, vol/EWMA, drawdowns, correlation (CI, rolling, lead/lag), HAC regression, robust-z anomalies, seasonality, walk-forward forecasts vs naive baselines, historical-beta scenarios; Quant Lab also runs the USDA price-vs-stocks-to-use model, with a switch between world and world-ex-China stocks to show how much that convention matters |
| Research engine (§8) | Planner → typed tools → evidence (E1…En) → narrative → number/citation verification → one revision pass. Claude Opus 5.5 via the Anthropic SDK with server-side refusal fallback; deterministic fallback. Briefs and reports in English, Spanish or Portuguese (`language`: Claude writes in that language with its number format, which verification understands; the deterministic engine writes English and says so). Follow-up questions (`parent_run_id`): an elliptical follow-up keeps the thread's subject, Claude sees the earlier questions and answer, and every figure is recomputed and verified in the new run |
| Reports (§12) | Versioned HTML, native PDF (WeasyPrint; page numbers, charts) and Excel (summary, evidence, sources, chart data). Read-only share links for people outside the workspace: 1–90 day expiry, revocable, token stored as a hash, views counted and audited; shared (and in-app) report HTML is served with a sandboxing Content-Security-Policy and no scripts |
| Monitoring (FR-009) | Alert rules (threshold, % move, anomaly), evaluated after each ingestion. Alerts fire once when a condition starts to hold and re-arm when it stops (unusual-move alerts once per observation), and can email their creator |
| Monitoring agents (FR-017, §21 Layer 3) | Saved agents watch instruments for unusual moves, large % moves, crowded CFTC positioning, high-volatility regimes or USDA revisions to world production or stocks (once per USDA report), or for new Fed/BEA/CFTC press releases matching keywords; checked after each data refresh; when one fires (outside a cooldown) it runs a research template and records the evidence, with optional email; Intelligence › Agents |
| Report templates (§12) | Daily Market Brief, Commodity Deep Dive, Global Macro Outlook, Country Intelligence, Cross-Asset Research |
| Public API (FR-018) | OpenAPI docs at `/docs` with Bearer API-key auth; quickstart on the Account page |
| Scheduled reports (FR-010) | Saved questions re-run daily / weekly / monthly by the scheduler, after the data refresh |
| Trade flows (FR-016) | Annual exports/imports/balance and HS-chapter product mix per pair; `get_trade_flows` tool and Global Economy card |
| Portfolio risk (FR-015) | Saved long/short portfolios per workspace: volatility, historical/parametric VaR and CVaR, risk contributions, correlation, factor betas (dollar, 10Y yield, Brent), historical stress tests (2008, COVID, 2022); `analyze_portfolio` tool and Portfolio page |
| Event studies (§11) | Average cumulative abnormal moves around Fed decisions (inferred from the effective fed funds rate since 2009) or custom dates; `run_event_study` tool, added automatically to rate-cut/hike scenario questions, and in Quant Lab |
| ML forecasting (§11) | In deep research (or `include_ml`), a gradient-boosting model on lagged changes joins the walk-forward forecast competition; it is selected only if it beats the naive baselines (on the current data it does not, and reports say so) |
| Watchlist (§13) | A shared workspace watchlist (up to 40 market or U.S. macro series) shown first on Overview; ☆ Watch on any instrument detail in Markets or Global Economy; reorder and remove in Overview's edit mode; `/api/v1/watchlist` |
| Onboarding (§17 phase 6) | "Get started" checklist on Overview computed from real workspace progress, with a one-click example portfolio; a "This week" briefing (next scheduled events and U.S. data due, the latest official releases, USDA stocks-to-use with revisions; `/api/v1/briefing`) |
| Supply and demand (FR-013) | USDA PSD balance sheets for sugar, coffee, corn, wheat, soybeans, cotton and rice by country and market year since 1990, through the same pipeline (raw payloads, quality checks, freshness); USDA revisions keep the previous value and report month. `get_fundamentals` gives world or country production, consumption, trade, ending stocks, stocks-to-use versus 20 years, top producers/exporters and revisions, plus a real-price vs stocks-to-use fit across market years (world, or world ex-China for grains, oilseeds and cotton by USDA convention) that reports the price the projection implies with an 80% band, or "no signal" when the fit is weak; a "Supply and demand" report section; `/api/v1/fundamentals`; a Markets card with a country view. Energy and metals return a recorded gap |
| Economic calendar (§8.2) | FOMC meetings parsed from the Federal Reserve Board's public calendar (refreshed daily; `atlas ingest --calendar`); official U.S. release dates from FRED when `FRED_API_KEY` is set, otherwise the next reference period due per U.S. series and never a guessed date. `get_economic_calendar` tool (Daily Market Brief, Global Macro Outlook, Fed and "what to watch" questions), `/api/v1/calendar`, Global Economy card |
| Official documents (§8.2) | Press-release feeds from the Federal Reserve Board, BEA and CFTC indexed every 6 hours (`atlas ingest --documents`); `search_documents` returns dated, linked releases as evidence and a "Recent official releases" report section; Intelligence › Releases. No commercial news wire is connected, and results say so |
| Volatility regimes (§11) | Realized-vol percentile buckets plus a 2-state Markov-switching model (`detect_regime`), in research and Quant Lab |
| Positioning & countries (FR-011, §9) | COT index, z-score and crowding flags per market; cross-country comparisons with stale-value flags; country profiles |
| API (§14) | FastAPI: instruments, quotes, history, macro (with `as_of`), data/pipeline status, research jobs (202 + polling), quant, reports, alerts |
| Data export (FR-018) | `GET /api/v1/datasets/{id}/export?format=csv|json` for every dataset kind (prices, macro, CFTC positioning, trade, USDA balances) with `start`/`end` filters and point-in-time `as_of` for macro; CSV opens with provenance comment lines (source, citation, observation and ingestion dates, freshness, licence note; `header=false` drops them); exports are audited. Download links on Markets, the USDA card and Data Explorer |
| UI (§13) | Next.js: Overview, AI Research, Markets, Global Economy, Quant Lab, Reports, Intelligence, Data Explorer |

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability, what ATLAS already does (hashed tokens, two-factor, workspace isolation, sandboxed report pages, signed billing webhooks, rate limits, audit log) and how to operate it securely. The Security workflow audits Python and npm dependencies on every push and weekly.

## Selling it

See [docs/monetization.md](docs/monetization.md) for an honest assessment of what it would take to sell ATLAS: data licensing, regulation, target niches, pricing and next steps.

## Honest limitations

- **Data is public and delayed.**
  - Commodity prices are IMF monthly-average spot benchmarks via FRED, not ICE/CME futures settlements.
  - FX rates are Fed H.10 reference rates.
  - Nothing here is a tradable quote, and the UI and reports say so.
- **Coverage gaps are disclosed, not filled.**
  - Gold and silver have no free redistributable price source, so their prices show as `no_source`. Their CFTC positioning is available.
  - World Bank data is annual and lagged; values far older than peers are flagged and excluded from rankings.
  - Supply-and-demand data covers USDA PSD agricultural commodities only; energy, metals and cocoa fundamentals and all futures curves return "not connected", recorded as evidence.
  - Document search covers official press releases only (Fed, BEA, CFTC); BLS blocks automated feed access. There is no news wire or sell-side research.
- **Stack differences from the spec:**
  - SQLite by default; Postgres/Timescale via `ATLAS_DATABASE_URL`.
  - A thread pool instead of Celery/Redis, and a polling scheduler instead of Dagster.
  - Plain CSS instead of Tailwind.
  - All interfaces are modular so these can be swapped.
- **Not built yet:**
  - higher-frequency non-US data (IMF/BIS connectors);
  - USDA/EIA connectors (need free API keys);
  - licensed market data;
  - self-service billing (plans are set by an operator with `atlas set-plan`).
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

See `.env.example`. Key variables: `ATLAS_DATABASE_URL`, `ATLAS_OBJECT_STORE`, `ATLAS_LLM` (`auto`/`off`), `ATLAS_LLM_MODEL` (default `claude-opus-5-5`), `ATLAS_LLM_EFFORT`, `ATLAS_SCHEDULER=on` (refresh inside the API process), `FRED_API_KEY` (optional; official U.S. data release dates), `NEXT_PUBLIC_API_URL` for the web app.

## Backups

```bash
atlas backup --out backups            # one .tar.gz: every table, report files, raw payloads (--no-raw to skip)
atlas restore backups/atlas-backup-20261023T153250Z.tar.gz --database-url postgresql+psycopg://...
```

Backups are database-neutral (each table as gzipped JSON lines), so a SQLite backup restores into PostgreSQL and back. Restore checks every file's SHA-256 against the manifest, rejects unsafe archive entries, and only writes into an empty database. The key that encrypts two-factor secrets (`ATLAS_SECRET_KEY` or `data/secret.key`) is never included: keep it separately, or users with two-factor sign in with a recovery code and re-enroll. In Docker: `docker compose exec api atlas backup --out /data/backups`.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on pull requests: the Python tests on 3.12 and 3.13 (SQLite) and again on TimescaleDB/PostgreSQL 16, a web type-check and production build, and a build of both Docker images with an API smoke test. To run the suite against Postgres locally, set `ATLAS_TEST_DATABASE_URL` (the database is wiped).

## Monitoring

`GET /metrics` serves Prometheus text format: HTTP request counts and latency histograms per route template, research runs by status and engine, 30-day pipeline runs by status, datasets by freshness, and LLM configuration. Set `ATLAS_METRICS_TOKEN` to require a bearer token. Outbound requests identify as `ATLAS_USER_AGENT` (default `ATLAS-research/0.1 (+contact URL)`); set your own contact URL in production.

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
