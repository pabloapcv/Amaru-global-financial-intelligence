"""Central bank policy rates: where each rate stands, each bank's latest move, and the global direction.

Rates are the daily BIS series, so a move is dated to the day the rate changed. The direction of a
bank compares its rate with six months earlier, and the global reading is the share of banks that
raised minus the share that cut, counting every bank once whatever the size of its economy.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select

from atlas.data.access import DataUnavailable, get_series
from atlas.data.db import MacroObservation, session_scope
from atlas.instruments import STALE_POLICY_RATE_DAYS, load_registry, policy_rate_id

DIRECTION_MONTHS = 6
MIN_BANKS = 10
RECENT_DAYS = 45  # long enough for a bank the BIS reports late
NOT_COVERED = {"ARG": "the BIS series stopped in June 2025", "BOL": "not in the BIS data", "ECU": "not in the BIS data",
               "URY": "not in the BIS data", "PRY": "not in the BIS data"}
CAVEAT = ("Rates are as the BIS reports them and arrive about a week late, so a decision in the past few days may be missing. "
          "Where a bank sets a range, the BIS shows the midpoint. A change in the rate a bank targets shows as a move. "
          "Every bank counts once, whatever the size of its economy.")


class PolicyRatesUnavailable(LookupError):
    pass


def _bp(x: float) -> float:
    return round(float(x) * 100, 1)


def _change(s: pd.Series, months: int) -> float | None:
    """Change in bp over `months`, or None if the series doesn't go back that far."""
    then = s[s.index <= s.index[-1] - pd.DateOffset(months=months)]
    return _bp(s.iloc[-1] - then.iloc[-1]) if len(then) else None


def _bank(area: dict[str, Any], s: pd.Series, today: date) -> dict[str, Any]:
    moves = s.diff().dropna()
    moves = moves[moves.abs() > 1e-9]
    last = moves.index[-1] if len(moves) else None
    year = moves[moves.index > s.index[-1] - pd.DateOffset(months=12)]
    direction = _change(s, DIRECTION_MONTHS)
    return {
        "area": area["code"], "name": area["name"], "countries": area["countries"], "dataset_id": policy_rate_id(area["code"]),
        "rate": round(float(s.iloc[-1]), 3), "as_of": s.index[-1].date().isoformat(),
        "stale": (today - s.index[-1].date()).days > STALE_POLICY_RATE_DAYS,
        "last_move": None if last is None else {"date": last.date().isoformat(), "change_bp": _bp(moves.iloc[-1])},
        "change_3m_bp": _change(s, 3), "change_12m_bp": _change(s, 12),
        "cuts_12m": int((year < 0).sum()), "hikes_12m": int((year > 0).sum()),
        "direction": None if direction is None else "cutting" if direction < 0 else "hiking" if direction > 0 else "on hold",
    }


def _net_path(series: dict[str, pd.Series]) -> list[dict[str, Any]]:
    """Share of banks higher than six months earlier minus the share lower, month by month, in %."""
    monthly = pd.concat({k: s.resample("ME").last() for k, s in series.items()}, axis=1).ffill(limit=2)
    change = monthly - monthly.shift(DIRECTION_MONTHS)
    known = change.notna().sum(axis=1)
    net = 100 * (change.gt(1e-9).sum(axis=1) - change.lt(-1e-9).sum(axis=1)) / known.where(known >= MIN_BANKS)
    return [{"date": d.date().isoformat(), "value": round(float(v), 1)} for d, v in net.dropna().items()]


def recent_moves(days: int = RECENT_DAYS, stored_since: datetime | None = None, today: date | None = None) -> list[dict[str, Any]]:
    """Moves dated in the past `days`, newest first.

    `stored_since` keeps only moves ATLAS stored or corrected after that time: the date of a move can't say
    what is new, because the BIS reports some banks weeks late.
    """
    cutoff = (today or date.today()) - timedelta(days=days)
    out = []
    for a in load_registry()["policy_rates"]["areas"]:
        sid = policy_rate_id(a["code"])
        try:
            s, _ = get_series(sid, start=cutoff - timedelta(days=120))
        except DataUnavailable:
            continue
        moves = s.diff().dropna()
        moves = moves[(moves.abs() > 1e-9) & (moves.index.date >= cutoff)]
        if moves.empty:
            continue
        if stored_since is not None:
            with session_scope() as db:
                stored = dict(db.execute(select(MacroObservation.period, func.max(MacroObservation.vintage_at))
                                         .where(MacroObservation.series_id == sid, MacroObservation.period >= cutoff)
                                         .group_by(MacroObservation.period)).all())
            moves = moves[[stored[d.date()] >= stored_since for d in moves.index]]
        out += [{"area": a["code"], "name": a["name"], "dataset_id": sid, "date": d.date().isoformat(), "change_bp": _bp(v),
                 "rate": round(float(s[d]), 3)} for d, v in moves.items()]
    return sorted(out, key=lambda m: m["date"], reverse=True)


def overview(countries: list[str] | None = None, today: date | None = None) -> dict[str, Any]:
    """Every bank's position and the global direction; `countries` (ISO3) narrows the table, not the direction."""
    today = today or date.today()
    areas = load_registry()["policy_rates"]["areas"]
    series: dict[str, pd.Series] = {}
    metas = {}
    for a in areas:
        try:
            s, meta = get_series(policy_rate_id(a["code"]))
        except DataUnavailable:
            continue
        s = s.dropna()
        if len(s):
            series[a["code"]], metas[a["code"]] = s, meta
    if not series:
        raise PolicyRatesUnavailable("no policy rates are stored yet")

    banks = [_bank(a, series[a["code"]], today) for a in areas if a["code"] in series]
    live = [b for b in banks if not b["stale"] and b["direction"]]
    wanted = set(countries or ())
    shown = [b for b in banks if not wanted or wanted & set(b["countries"])]
    covered = {c for b in banks for c in b["countries"]}
    return {
        "as_of": max(b["as_of"] for b in banks), "banks": sorted(shown, key=lambda b: b["name"]),
        "direction": {"months": DIRECTION_MONTHS, "banks": len(live), "cutting": sum(b["direction"] == "cutting" for b in live),
                      "hiking": sum(b["direction"] == "hiking" for b in live), "on_hold": sum(b["direction"] == "on hold" for b in live),
                      "path": _net_path(series)},
        "not_covered": [{"country": c, "reason": NOT_COVERED.get(c, "not in the BIS data")} for c in sorted(wanted - covered)],
        "sources": [metas[b["area"]] for b in shown] or list(metas.values()), "caveat": CAVEAT,
    }
