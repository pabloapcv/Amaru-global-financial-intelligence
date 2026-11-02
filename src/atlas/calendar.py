"""Economic calendar (spec §8.2 get_economic_calendar).

Sources, all official:
- FOMC meeting dates, parsed from the Federal Reserve Board's public meeting calendar (no key).
- U.S. statistical release dates from the FRED API, when FRED_API_KEY is set.

Without a FRED key, each tracked U.S. macro series still shows which reference period is due
next (from its last stored observation and frequency), but no release date: Amaru never guesses one.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd
from sqlalchemy import delete, func, select

from atlas.config import settings
from atlas.data.db import CalendarEvent, session_scope, utcnow

log = logging.getLogger("atlas.calendar")

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FRED_API = "https://api.stlouisfed.org/fred"
REFRESH_HOURS = 24
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
MONTHS_ABBR = {m[:3]: i for m, i in MONTHS.items()}

_PANEL_RE = re.compile(r"(\d{4}) FOMC Meetings</a></h4>(.*?)(?=\d{4} FOMC Meetings</a></h4>|$)", re.S)
_MEETING_RE = re.compile(
    r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<', re.S)


def _client() -> httpx.Client:
    return httpx.Client(timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent},
                        follow_redirects=True)


def parse_fomc(html: str) -> list[dict[str, Any]]:
    """Scheduled FOMC meetings from the Board's calendar page. Notation votes and unscheduled
    meetings are skipped; '*' marks meetings with a Summary of Economic Projections."""
    out: list[dict[str, Any]] = []
    for year_s, body in _PANEL_RE.findall(html):
        year = int(year_s)
        for month_s, date_s in _MEETING_RE.findall(body):
            m = re.fullmatch(r"\s*(\d{1,2})(?:\s*-\s*(\d{1,2}))?(\*?)\s*", date_s)
            months = [MONTHS.get(p.strip().lower()) or MONTHS_ABBR.get(p.strip().lower()[:3]) for p in month_s.split("/")]
            if not m or None in months:
                continue  # e.g. "22 (notation vote)", "(unscheduled)"
            d1, d2, sep = int(m.group(1)), m.group(2), bool(m.group(3))
            start = date(year, months[0], d1)
            end = start
            if d2:
                end_month = months[-1] if len(months) > 1 else months[0]
                end = date(year + (end_month < months[0]), end_month, int(d2))
            out.append({"source": "fomc", "kind": "fomc_meeting", "title": "FOMC meeting" + (" (with projections)" if sep else ""),
                        "start_date": start, "end_date": end,
                        "detail": {"summary_of_economic_projections": sep, "decision_date": end.isoformat()},
                        "url": FOMC_URL})
    return out


def fetch_fred_releases(series: list[tuple[str, str, str]], api_key: str, client: httpx.Client,
                        today: date | None = None) -> list[dict[str, Any]]:
    """Upcoming official release dates for the releases containing the given (dataset_id, name, fred_symbol)."""
    today = today or date.today()
    base = {"api_key": api_key, "file_type": "json"}
    releases: dict[int, dict[str, Any]] = {}
    for dsid, name, symbol in series:
        r = client.get(f"{FRED_API}/series/release", params={**base, "series_id": symbol})
        r.raise_for_status()
        for rel in r.json().get("releases", [])[:1]:
            entry = releases.setdefault(rel["id"], {"name": rel["name"], "link": rel.get("link"), "datasets": []})
            entry["datasets"].append({"dataset_id": dsid, "name": name})
    out = []
    for rid, rel in releases.items():
        r = client.get(f"{FRED_API}/release/dates", params={
            **base, "release_id": rid, "realtime_start": today.isoformat(), "realtime_end": "9999-12-31",
            "include_release_dates_with_no_data": "true", "sort_order": "asc", "limit": 24})
        r.raise_for_status()
        for d in r.json().get("release_dates", []):
            day = date.fromisoformat(d["date"])
            if day >= today:
                out.append({"source": "fred_releases", "kind": "data_release", "title": rel["name"],
                            "start_date": day, "end_date": None,
                            "detail": {"release_id": rid, "datasets": rel["datasets"]},
                            "url": rel.get("link") or f"https://fred.stlouisfed.org/releases/{rid}"})
    return out


def _us_macro() -> list[tuple[str, str, str]]:
    from atlas.instruments import datasets

    return [(d.dataset_id, d.name, d.provider_symbol) for d in datasets().values()
            if d.kind == "macro" and d.provider_id == "fred" and d.meta.get("country") == "US"]


def _replace(source: str, events: list[dict[str, Any]]) -> None:
    with session_scope() as s:
        s.execute(delete(CalendarEvent).where(CalendarEvent.source == source))
        now = utcnow()
        for e in events:
            s.add(CalendarEvent(fetched_at=now, **e))


def refresh(client: httpx.Client | None = None) -> dict[str, Any]:
    """Re-fetch every calendar source. A failing source keeps its previously stored events."""
    client = client or _client()
    status: dict[str, Any] = {}
    try:
        r = client.get(FOMC_URL)
        r.raise_for_status()
        events = parse_fomc(r.text)
        if not events:
            raise ValueError("no meetings found; the page layout may have changed")
        _replace("fomc", events)
        status["fomc"] = {"ok": True, "events": len(events)}
    except Exception as exc:  # noqa: BLE001 — keep last good copy, report the failure
        log.warning("FOMC calendar refresh failed: %s", exc)
        status["fomc"] = {"ok": False, "error": str(exc)}
    if settings.fred_api_key:
        try:
            events = fetch_fred_releases(_us_macro(), settings.fred_api_key, client)
            _replace("fred_releases", events)
            status["fred_releases"] = {"ok": True, "events": len(events)}
        except Exception as exc:  # noqa: BLE001
            # httpx errors include the request URL, which carries the API key: never log it.
            log.warning("FRED release-date refresh failed: %s", type(exc).__name__)
            status["fred_releases"] = {"ok": False, "error": type(exc).__name__}
    else:
        status["fred_releases"] = {"ok": False, "error": "FRED_API_KEY not set"}
    return status


def last_fetched() -> datetime | None:
    with session_scope() as s:
        return s.execute(select(func.max(CalendarEvent.fetched_at))).scalar()


def refresh_if_due() -> dict[str, Any] | None:
    last = last_fetched()
    if last is None or utcnow().replace(tzinfo=None) - last.replace(tzinfo=None) > timedelta(hours=REFRESH_HOURS):
        return refresh()
    return None


def _next_period(last: date, frequency: str) -> str | None:
    ts = pd.Timestamp(last)
    if frequency == "monthly":
        return (ts + pd.DateOffset(months=1)).strftime("%Y-%m")
    if frequency == "quarterly":
        q = (ts + pd.DateOffset(months=3)).to_period("Q")
        return f"{q.year}-Q{q.quarter}"
    return None


def upcoming(days: int = 60, today: date | None = None) -> dict[str, Any]:
    """Events from `today` through `today + days`, plus the next reference period due for each
    tracked U.S. macro series."""
    from atlas.data.access import series_meta

    today = today or date.today()
    horizon = today + timedelta(days=days)
    with session_scope() as s:
        rows = s.execute(select(CalendarEvent).where(
            func.coalesce(CalendarEvent.end_date, CalendarEvent.start_date) >= today,
            CalendarEvent.start_date <= horizon).order_by(CalendarEvent.start_date)).scalars().all()
        events = [{"date": r.start_date.isoformat(), "end_date": r.end_date.isoformat() if r.end_date else None,
                   "kind": r.kind, "title": r.title, "source": r.source, "detail": r.detail, "url": r.url,
                   "fetched_at": r.fetched_at.isoformat(timespec="seconds")} for r in rows]
        sources = {src: fetched.isoformat(timespec="seconds") for src, fetched in s.execute(
            select(CalendarEvent.source, func.max(CalendarEvent.fetched_at)).group_by(CalendarEvent.source)).all()}
    dated = {d["dataset_id"] for e in events if e["kind"] == "data_release" for d in e["detail"].get("datasets", [])}
    due = []
    for dsid, name, _ in _us_macro():
        try:
            meta = series_meta(dsid)
        except Exception:  # noqa: BLE001 — not ingested yet
            continue
        if not meta.last_observation:
            continue
        last = date.fromisoformat(str(meta.last_observation)[:10])
        due.append({"dataset_id": dsid, "name": name, "frequency": meta.frequency,
                    "last_period": last.isoformat(), "next_period_due": _next_period(last, meta.frequency),
                    "official_release_date": next((e["date"] for e in events if e["kind"] == "data_release"
                                                   and any(d["dataset_id"] == dsid for d in e["detail"].get("datasets", []))), None)})
    notes = []
    if "fomc" not in sources:
        notes.append("FOMC meeting dates have not been fetched yet.")
    if not settings.fred_api_key:
        notes.append("Official U.S. data release dates need a free FRED API key (FRED_API_KEY); without it only the next "
                     "reference period due is shown, never an estimated date.")
    elif not dated:
        notes.append("No FRED release dates are stored yet.")
    return {"from": today.isoformat(), "to": horizon.isoformat(), "events": events, "series_due": due,
            "sources": sources, "notes": notes}
