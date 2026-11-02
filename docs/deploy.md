# Deploying ATLAS

This guide puts ATLAS on one Linux server with HTTPS, PostgreSQL (TimescaleDB), the scheduler and daily backups. It works with any provider that gives you a virtual machine: Hetzner, DigitalOcean, AWS Lightsail, a Google Cloud VM, and so on.

## What you need

- A server with 2 vCPUs, 4 GB RAM and 40 GB of disk (Ubuntu 24.04 or similar), with Docker Engine and Docker Compose 2.24 or later.
- A domain. Create two DNS records pointing at the server: `atlas.example.com` (the web app) and `api.atlas.example.com` (the API).
- Ports 80 and 443 open to the internet.
- An SMTP account for email, for example Postmark, Amazon SES or Resend. Without one, sign-up verification, password resets, alerts and newsletters only reach the logs.
- An Anthropic API key. Optional: without it, research uses the deterministic writer.

## 1. Configure

```bash
git clone https://github.com/pabloapcv/atlas-fin.git atlas && cd atlas
cp .env.example .env
```

Set at least these in `.env`:

| Setting | Value |
|---|---|
| `ATLAS_DOMAIN` | `atlas.example.com` (the API is served at `api.` in front of it) |
| `ACME_EMAIL` | your email, for Let's Encrypt certificate notices |
| `POSTGRES_PASSWORD` | a long random password |
| `ATLAS_SECRET_KEY` | `openssl rand -hex 32`. Keep a copy somewhere safe and separate: it encrypts two-factor secrets and webhook signing keys, and backups deliberately don't include it |
| `ANTHROPIC_API_KEY` | your key |
| `ATLAS_SMTP_HOST`, `ATLAS_SMTP_PORT`, `ATLAS_SMTP_USER`, `ATLAS_SMTP_PASSWORD`, `ATLAS_SMTP_FROM` | your email provider's settings; the sender should be on your domain |
| `ATLAS_OPERATOR_EMAILS` | your email (for the operator console; turn on two-factor sign-in) |
| `ATLAS_METRICS_TOKEN` | a random token, if you scrape `/metrics` |
| `ATLAS_REQUIRE_EMAIL_VERIFICATION` | `true` |

When you sell plans, also set:

- `ATLAS_COMMERCIAL_MODE=true`
- the `STRIPE_*` settings
- `ATLAS_PLAN_PRICES`
- `ATLAS_TERMS_URL` and `ATLAS_PRIVACY_URL`

See [monetization.md](monetization.md) for why.

The production overlay sets these for you from `ATLAS_DOMAIN`, so don't set them yourself: `ATLAS_PUBLIC_URL`, `ATLAS_API_PUBLIC_URL`, `ATLAS_CORS_ORIGINS` and `ATLAS_COOKIE_SECURE`.

## 2. Start

```bash
docker compose -f infrastructure/docker/docker-compose.yml -f infrastructure/docker/docker-compose.prod.yml --env-file .env up -d --build
```

Caddy obtains the HTTPS certificates on first start, which takes about a minute once DNS points at the server.

Define a shorthand to keep the commands short:

```bash
alias atlas-prod='docker compose -f infrastructure/docker/docker-compose.yml -f infrastructure/docker/docker-compose.prod.yml --env-file .env'
```

## 3. Check and load data

```bash
atlas-prod exec api atlas doctor     # must end with 0 errors
atlas-prod exec api atlas ingest     # first backfill; the scheduler keeps data fresh afterwards
atlas-prod exec api atlas create-user you@example.com --plan pro
```

Then sign in at `https://atlas.example.com`, turn on two-factor sign-in on the Account page, and open the operator console. Its Configuration card repeats the doctor's report.

To sell, see [monetization.md](monetization.md). The Stripe webhook endpoint is `https://api.atlas.example.com/api/v1/billing/webhook`.

## Backups

The `backup` service writes a portable archive every day into the `atlas-backups` volume and keeps the newest 14 (`ATLAS_BACKUP_KEEP`).

**Copy them off the server.** A backup on the same disk doesn't survive losing the server. First, find the folder on the server where the volume lives:

```bash
docker volume inspect $(docker volume ls -q | grep atlas-backups) --format '{{ .Mountpoint }}'
```

Then copy that folder from another machine with a nightly cron job, for example `rsync -a server:<that folder>/ ~/atlas-backups/`.

Restore into a fresh deployment's empty database:

```bash
atlas-prod cp atlas-backup-….tar.gz api:/tmp/
atlas-prod exec api atlas restore /tmp/atlas-backup-….tar.gz
```

Set the same `ATLAS_SECRET_KEY` first. Without it, two-factor users sign in with a recovery code and re-enroll, and webhook secrets must be rotated.

## Updating

```bash
git pull
atlas-prod up -d --build
atlas-prod exec api atlas doctor
```

Schema changes apply automatically on start.

## Scaling notes

- **Scheduler:** one scheduler runs at a time (a database lease enforces this), so extra replicas wait.
- **API rate limits:** by default each API process counts on its own. With several API replicas, set `ATLAS_RATE_LIMIT_STORE=database` so they share one count in the database (one or two small writes per request).
- **Reports and raw files:** these live in the `atlas-objects` volume. With several servers, use shared storage.
