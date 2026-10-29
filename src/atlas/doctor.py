"""`atlas doctor`: is this deployment configured safely and completely?

Each check returns a level (error, warning, info, ok), what it found, and what to do. A deployment
counts as production when ATLAS_PUBLIC_URL is https (or --production is passed); some settings that
are fine on a laptop are errors there. Nothing here prints secret values.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

LEVELS = ("error", "warning", "info", "ok")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _true(key: str, default: str = "false") -> bool:
    return _env(key, default).lower() in ("1", "true", "yes")


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def _local(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def run(production: bool | None = None, *, check_database: bool = True) -> dict[str, Any]:
    from atlas import config

    public = _env("ATLAS_PUBLIC_URL", "http://localhost:3010")
    api_public = _env("ATLAS_API_PUBLIC_URL", "http://localhost:8010")
    prod = production if production is not None else public.startswith("https://")
    checks: list[dict[str, str]] = []

    def add(level: str, key: str, found: str, fix: str = "") -> None:
        checks.append({"level": level, "check": key, "found": found, "fix": fix})

    bad = "error" if prod else "warning"

    # Accounts and sessions
    if _env("ATLAS_AUTH", "required").lower() == "off":
        add(bad if prod else "info", "Sign-in", "ATLAS_AUTH=off: anyone who can reach the API uses the local workspace",
            "Set ATLAS_AUTH=required for any shared deployment.")
    else:
        add("ok", "Sign-in", "required")
    secret = _env("ATLAS_SECRET_KEY")
    if not secret:
        add(bad, "Encryption key", "ATLAS_SECRET_KEY is not set; a key file on the data volume is used",
            "Set a long random ATLAS_SECRET_KEY (e.g. `openssl rand -hex 32`) and keep it out of backups.")
    elif len(secret) < 32:
        add("warning", "Encryption key", f"ATLAS_SECRET_KEY is short ({len(secret)} characters)", "Use at least 32 random characters.")
    else:
        add("ok", "Encryption key", "set")
    if prod and not _true("ATLAS_COOKIE_SECURE"):
        add("error", "Secure cookies", "ATLAS_COOKIE_SECURE is not true on an https deployment", "Set ATLAS_COOKIE_SECURE=true.")
    elif prod:
        add("ok", "Secure cookies", "on")
    if _true("ATLAS_ALLOW_SIGNUP", "true") and not _true("ATLAS_REQUIRE_EMAIL_VERIFICATION"):
        add("warning" if prod else "info", "Email verification", "self-service sign-up is on but email verification is optional",
            "Set ATLAS_REQUIRE_EMAIL_VERIFICATION=true so unverified accounts cannot run research.")

    # URLs and CORS
    origins = [o.strip() for o in config.settings.cors_origins if o.strip()]
    if _origin(public) and _origin(public) not in origins:
        add("error", "CORS", f"the web app's origin {_origin(public)} is not in ATLAS_CORS_ORIGINS", "Add it to ATLAS_CORS_ORIGINS.")
    elif prod and any(_local(o) for o in origins):
        add("warning", "CORS", "ATLAS_CORS_ORIGINS still allows localhost", "List only the web app's origin.")
    else:
        add("ok", "CORS", ", ".join(origins))
    if prod and (_local(public) or _local(api_public)):
        add("error", "Public URLs", "ATLAS_PUBLIC_URL or ATLAS_API_PUBLIC_URL points at localhost",
            "Set both to the addresses people use; email links (verification, reset, unsubscribe) are built from them.")
    elif prod and not api_public.startswith("https://"):
        add("error", "Public URLs", "ATLAS_API_PUBLIC_URL is not https", "Serve the API over https too.")
    else:
        add("ok", "Public URLs", f"web {public}, API {api_public}")

    # Database and storage
    db = config.settings.database_url
    if db.startswith("sqlite"):
        add("warning" if prod else "info", "Database", "SQLite", "Use PostgreSQL (TimescaleDB) in production; the compose files set it up.")
    else:
        add("ok", "Database", db.split("://", 1)[0])
    if check_database:
        try:
            from sqlalchemy import text

            from atlas.data.db import get_engine

            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            add("ok", "Database connection", "reachable")
        except Exception as exc:
            add("error", "Database connection", f"cannot connect: {type(exc).__name__}", "Check ATLAS_DATABASE_URL and that the database is running.")

    # Email
    if not _env("ATLAS_SMTP_HOST"):
        add(bad, "Email", "SMTP is not configured: verification, password-reset, alert, digest and newsletter emails are only logged",
            "Set ATLAS_SMTP_HOST, ATLAS_SMTP_PORT, ATLAS_SMTP_USER, ATLAS_SMTP_PASSWORD and ATLAS_SMTP_FROM.")
    elif not _env("ATLAS_SMTP_FROM"):
        add("warning", "Email", "ATLAS_SMTP_FROM is not set (mail goes out as no-reply@localhost)", "Set a sender on your domain.")
    else:
        add("ok", "Email", f"SMTP via {_env('ATLAS_SMTP_HOST')}")

    # Claude
    from atlas.research import llm

    if llm.available():
        add("ok", "Claude", f"configured ({config.settings.llm_model})")
    else:
        add("warning", "Claude", "no Anthropic credentials: research uses the deterministic writer only", "Set ANTHROPIC_API_KEY.")

    # Operations
    ops = [e for e in _env("ATLAS_OPERATOR_EMAILS").split(",") if e.strip()]
    add("ok" if ops else ("warning" if prod else "info"), "Operators", f"{len(ops)} configured" if ops else "none",
        "" if ops else "List your email in ATLAS_OPERATOR_EMAILS (and turn on two-factor) to use the operator console.")
    if prod and not _env("ATLAS_METRICS_TOKEN"):
        add("warning", "Metrics", "/metrics is public", "Set ATLAS_METRICS_TOKEN.")
    if _true("ATLAS_WEBHOOKS_ALLOW_PRIVATE"):
        add(bad if prod else "info", "Webhooks", "ATLAS_WEBHOOKS_ALLOW_PRIVATE lets webhooks reach private addresses and plain http",
            "Leave it off outside local testing.")

    # Billing and data licences
    stripe = {k: bool(_env(k)) for k in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_INVESTOR", "STRIPE_PRICE_RESEARCH_PRO", "STRIPE_PRICE_PRO")}
    if any(stripe.values()) and not all(stripe.values()):
        add("error", "Billing", "Stripe is partly configured; missing " + ", ".join(k for k, v in stripe.items() if not v),
            "Set all STRIPE_* settings, or none.")
    elif all(stripe.values()):
        add("ok", "Billing", "Stripe configured" + (" (test mode)" if _env("STRIPE_SECRET_KEY").startswith("sk_test_") else ""))
    else:
        add("info", "Billing", "self-service billing is off; operators change plans", "Set the STRIPE_* settings to sell plans.")
    selling = all(stripe.values()) or bool(_env("ATLAS_PLAN_PRICES"))
    if selling and not config.settings.commercial_mode:
        add("warning", "Data licences", "you are selling plans but ATLAS_COMMERCIAL_MODE is off, so restricted third-party series are served",
            "Set ATLAS_COMMERCIAL_MODE=true (see docs/monetization.md).")
    if not _env("ATLAS_TERMS_URL") or not _env("ATLAS_PRIVACY_URL"):
        add("warning" if prod and selling else "info", "Legal pages", "ATLAS_TERMS_URL and ATLAS_PRIVACY_URL are not both set",
            "Publish terms of service and a privacy policy (have a lawyer review them) and set both URLs.")
    if not config.settings.fred_api_key:
        add("info", "FRED key", "not set: the calendar lacks official U.S. release dates", "Optional: set FRED_API_KEY (free).")

    counts = {lv: sum(1 for c in checks if c["level"] == lv) for lv in LEVELS}
    order = {lv: i for i, lv in enumerate(LEVELS)}
    return {"production": prod, "ok": counts["error"] == 0, "counts": counts,
            "checks": sorted(checks, key=lambda c: order[c["level"]])}


def format_text(report: dict[str, Any]) -> str:
    mark = {"error": "✗", "warning": "!", "info": "·", "ok": "✓"}
    lines = [f"ATLAS doctor ({'production' if report['production'] else 'development'} checks)", ""]
    for c in report["checks"]:
        lines.append(f"{mark[c['level']]} {c['check']}: {c['found']}")
        if c["fix"] and c["level"] != "ok":
            lines.append(f"    → {c['fix']}")
    n = report["counts"]
    lines += ["", f"{n['error']} error(s), {n['warning']} warning(s), {n['info']} note(s)."]
    return "\n".join(lines)
