"""'This week' briefing for the Overview page: scheduled events, the latest official releases and the
latest USDA balance sheets, from data Amaru already stores (nothing is fetched on request)."""

from __future__ import annotations

from typing import Any

from atlas import analytics, calendar, documents, policy_rates
from atlas.data.access import DataUnavailable, get_fundamentals, series_meta
from atlas.instruments import load_registry

_usda_cache: dict[str, tuple[str | None, dict[str, Any]]] = {}


def _usda(fid: str) -> dict[str, Any] | None:
    """World summary for one commodity, cached until the dataset is re-ingested."""
    meta = series_meta(fid)
    hit = _usda_cache.get(fid)
    if hit and hit[0] == meta.last_ingested_at:
        return hit[1]
    try:
        df, _ = get_fundamentals(fid)
    except DataUnavailable:
        return None
    r = analytics.fundamentals_balance(df, unit=meta.unit)
    revs = sorted(r["revisions_vs_previous_report"], key=lambda v: abs(v["change_pct"] or 0), reverse=True)
    out = {"dataset_id": fid, "usda_release": r["usda_release"], "market_year": r["latest_market_year"],
           "stocks_to_use_pct": r["stocks_to_use"]["latest_pct"], "stocks_to_use_percentile": r["stocks_to_use"]["percentile"],
           "stocks_to_use_change_pp": r["change_vs_prior_year"].get("stocks_to_use_change_pp"),
           "largest_revision": revs[0] if revs else None}
    _usda_cache[fid] = (meta.last_ingested_at, out)
    return out


def week(days: int = 14) -> dict[str, Any]:
    cal = calendar.upcoming(days)
    docs = documents.search("", days=7, limit=5)
    usda = []
    for c in load_registry().get("fundamentals", {}).get("commodities", []):
        s = _usda(f"FUND.{c['key']}")
        if s:
            usda.append({"name": c["name"], "instrument_id": c.get("instrument_id"), **s})
    usda.sort(key=lambda u: u["usda_release"], reverse=True)
    return {
        "events": [{k: e[k] for k in ("date", "end_date", "kind", "title", "url")} for e in cal["events"][:5]],
        "series_due": [{"name": d["name"], "next_period_due": d["next_period_due"], "official_release_date": d["official_release_date"]}
                       for d in cal["series_due"]],
        "calendar_window": {"from": cal["from"], "to": cal["to"]},
        "releases": [{k: d[k] for k in ("title", "publisher", "published", "url")} for d in docs["results"]],
        "usda": usda,
        "policy_moves": policy_rates.recent_moves(days=30)[:6],
    }
