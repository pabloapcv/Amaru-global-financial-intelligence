"""U.S. energy inventories and flows from the EIA's weekly reports.

Stocks are judged the way the EIA itself presents them: against the average and range of the same
week in the previous five years, because inventories swing with the seasons (gasoline builds before
summer, gas storage before winter). Flows are noisy week to week, so they use 4-week averages.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from atlas.data.access import DataUnavailable, get_series

STOCKS = [("Crude oil (excluding SPR)", "US.EIA.CRUDE_STOCKS", "million barrels", 1000.0),
          ("Gasoline", "US.EIA.GASOLINE_STOCKS", "million barrels", 1000.0),
          ("Distillates", "US.EIA.DISTILLATE_STOCKS", "million barrels", 1000.0),
          ("Natural gas in storage", "US.EIA.NATGAS_STORAGE", "Bcf", 1.0)]
SPR = "US.EIA.SPR_STOCKS"
FLOWS = [("Crude oil production", "US.EIA.CRUDE_PRODUCTION", "thousand barrels a day"),
         ("Crude oil imports", "US.EIA.CRUDE_IMPORTS", "thousand barrels a day"),
         ("Refinery utilization", "US.EIA.REFINERY_UTILIZATION", "%")]
YEARS = 5
TOLERANCE = pd.Timedelta(days=4)  # weekly data: the same week a year earlier is within 3-4 days of the date
CHART_WEEKS = 156
COVERS = {"CMD.CRUDE.WTI": "oil", "CMD.CRUDE.BRENT": "oil", "CMD.NATGAS.HH": "gas"}
CAVEAT = ("U.S. data only, from the EIA's weekly reports, which are estimates later revised by its monthly surveys. "
          "Stocks are compared with the same week of the previous five years. Crude stocks exclude the Strategic Petroleum Reserve. "
          "Inventories describe supply and demand; they don't forecast prices.")


class EnergyUnavailable(LookupError):
    pass


def covers(text_or_id: str) -> str | None:
    """"oil" or "gas" when a commodity question can be answered with EIA inventories, else None."""
    if text_or_id in COVERS:
        return COVERS[text_or_id]
    t = text_or_id.lower()
    if re.search(r"\b(crude|wti|brent|petroleum|gasoline|diesel|distillates?|heating oil)\b", t) \
            or re.search(r"(?<!palm )(?<!soybean )(?<!soy )(?<!olive )(?<!vegetable )\boil\b", t):
        return "oil"
    if re.search(r"natural gas|nat ?gas|henry hub|gas storage", t):
        return "gas"
    return None


def same_week(s: pd.Series, when: pd.Timestamp, years_back: int) -> float | None:
    """Value in the week nearest the same date `years_back` years earlier, if there is one within a few days."""
    target = when - pd.DateOffset(years=years_back)
    i = s.index.get_indexer([target], method="nearest")[0]
    return float(s.iloc[i]) if i >= 0 and abs(s.index[i] - target) <= TOLERANCE else None


def seasonal(s: pd.Series, when: pd.Timestamp) -> tuple[float, float, float] | None:
    """Average, low and high of the same week in each of the previous five years, or None if any year is missing."""
    past = [same_week(s, when, k) for k in range(1, YEARS + 1)]
    if any(v is None for v in past):
        return None
    vals = np.array(past, dtype=float)
    return float(vals.mean()), float(vals.min()), float(vals.max())


def _stock(name: str, dataset_id: str, unit: str, divisor: float, s: pd.Series) -> dict[str, Any] | None:
    last = s.index[-1]
    season = seasonal(s, last)
    if season is None or len(s) < 2:
        return None
    avg, low, high = season
    now = float(s.iloc[-1])
    year_ago = same_week(s, last, 1)
    vs_avg = 100 * (now / avg - 1)
    return {
        "name": name, "dataset_id": dataset_id, "unit": unit, "as_of": last.date().isoformat(),
        "level": round(now / divisor, 1), "weekly_change": round((now - float(s.iloc[-2])) / divisor, 1),
        "year_ago": None if year_ago is None else round(year_ago / divisor, 1),
        "five_year_average": round(avg / divisor, 1), "five_year_low": round(low / divisor, 1), "five_year_high": round(high / divisor, 1),
        "vs_five_year_average_pct": round(vs_avg, 1),
        "position": "above the five-year range" if now > high else "below the five-year range" if now < low
                    else "above the five-year average" if now > avg else "below the five-year average",
    }


def _deviation_path(s: pd.Series) -> list[dict[str, Any]]:
    out = []
    for when, v in s.iloc[-CHART_WEEKS:].items():
        season = seasonal(s, when)
        if season:
            out.append({"date": when.date().isoformat(), "value": round(100 * (float(v) / season[0] - 1), 1)})
    return out


def _flow(name: str, dataset_id: str, unit: str, s: pd.Series) -> dict[str, Any] | None:
    avg4 = s.rolling(4).mean().dropna()
    if avg4.empty:
        return None
    last = avg4.index[-1]
    year_ago = same_week(avg4, last, 1)
    now = float(avg4.iloc[-1])
    return {
        "name": name, "dataset_id": dataset_id, "unit": unit, "as_of": last.date().isoformat(), "latest_week": round(float(s.iloc[-1]), 1),
        "four_week_average": round(now, 1), "four_week_average_year_ago": None if year_ago is None else round(year_ago, 1),
        "change_vs_year_ago": None if year_ago is None else round(now - year_ago, 1),
    }


def overview(kind: str | None = None) -> dict[str, Any]:
    """Stocks against their seasonal norms, the SPR, and supply flows. `kind` "oil" or "gas" narrows it."""
    series: dict[str, pd.Series] = {}
    metas = []
    for dataset_id in [d for _, d, _, _ in STOCKS] + [SPR] + [d for _, d, _ in FLOWS]:
        try:
            s, meta = get_series(dataset_id)
        except DataUnavailable:
            continue
        series[dataset_id] = s.dropna()
        metas.append(meta)
    gas = {"US.EIA.NATGAS_STORAGE"}
    wanted = (lambda d: d in gas) if kind == "gas" else (lambda d: d not in gas) if kind == "oil" else (lambda d: True)
    stocks = [row for name, d, unit, div in STOCKS if d in series and wanted(d) and (row := _stock(name, d, unit, div, series[d]))]
    flows = [row for name, d, unit in FLOWS if d in series and wanted(d) and (row := _flow(name, d, unit, series[d]))]
    if not stocks and not flows:
        raise EnergyUnavailable("EIA inventories aren't loaded")
    spr = None
    if SPR in series and wanted(SPR) and len(series[SPR]) > 1:
        s = series[SPR]
        year = same_week(s, s.index[-1], 1)
        spr = {"dataset_id": SPR, "as_of": s.index[-1].date().isoformat(), "level": round(float(s.iloc[-1]) / 1000, 1),
               "unit": "million barrels", "change_12m": None if year is None else round((float(s.iloc[-1]) - year) / 1000, 1)}
    chart = [{"name": row["name"], "points": _deviation_path(series[row["dataset_id"]])} for row in stocks]
    used = {r["dataset_id"] for r in stocks + flows} | ({SPR} if spr else set())
    return {"stocks": stocks, "spr": spr, "flows": flows, "chart": [c for c in chart if c["points"]], "caveat": CAVEAT,
            "sources": [m for m in metas if m.dataset_id in used]}
