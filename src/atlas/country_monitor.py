"""Country monitor: the latest monthly and quarterly readings per country, beside the IMF's forecasts.

World Bank annual data lags a year or two. This fills the gap with what moves faster: monthly
inflation (IMF), the real effective exchange rate (BIS, monthly), the credit-to-GDP gap and real
house prices (BIS, quarterly), and the IMF World Economic Outlook's projections.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from atlas.data.access import DataUnavailable, SeriesMeta, get_series
from atlas.instruments import countries, country_extra_id, country_series_specs

CREDIT_GAP_WARNING = 10.0  # Basel III: a gap above 10 percentage points signals a credit boom
WEO_KEYS = ["WEO_GDP_GROWTH", "WEO_INFLATION", "WEO_UNEMPLOYMENT", "WEO_DEBT", "WEO_CURRENT_ACCOUNT"]
CAVEAT = ("Inflation is the IMF's year-on-year figure from national consumer price indexes; the exchange rate, credit gap and "
          "house prices are BIS series, and the forecasts are the IMF World Economic Outlook's. Countries publish on different "
          "schedules, so each figure carries its own date. Credit gaps are revised as their trend is re-estimated, and house price "
          "indexes cover different things in different countries. IMF projections are forecasts, not outcomes.")


class MonitorUnavailable(LookupError):
    pass


def _load(iso3: str, key: str, metas: list[SeriesMeta]) -> pd.Series | None:
    spec = next(s for s in country_series_specs() if s["key"] == key)
    if iso3 not in spec["countries"]:
        return None
    try:
        s, meta = get_series(country_extra_id(spec, iso3))
    except DataUnavailable:
        return None
    metas.append(meta)
    return s.dropna()


def _ago(s: pd.Series, months: int) -> float | None:
    then = s[s.index <= s.index[-1] - pd.DateOffset(months=months)]
    return float(then.iloc[-1]) if len(then) else None


def _quarter(d: pd.Timestamp) -> str:
    return f"{d.year} Q{(d.month - 1) // 3 + 1}"


def snapshot(iso3: str, today: date | None = None) -> tuple[dict[str, Any], list[SeriesMeta]]:
    """One country's readings; a section is None where the source has no data for it."""
    year = (today or date.today()).year
    metas: list[SeriesMeta] = []
    name = next(c["name"] for c in countries() if c["iso3"] == iso3)
    out: dict[str, Any] = {"country": iso3, "name": name}

    cpi = _load(iso3, "CPI_YOY", metas)
    out["inflation"] = None if cpi is None or cpi.empty else {
        "month": cpi.index[-1].strftime("%Y-%m"), "yoy_pct": round(float(cpi.iloc[-1]), 1),
        "three_months_earlier": None if (v := _ago(cpi, 3)) is None else round(v, 1),
        "a_year_earlier": None if (v := _ago(cpi, 12)) is None else round(v, 1),
    }
    reer = _load(iso3, "REER", metas)
    if reer is not None and len(reer) > 12:
        last = float(reer.iloc[-1])
        ten = reer[reer.index > reer.index[-1] - pd.DateOffset(years=10)]
        out["real_exchange_rate"] = {
            "month": reer.index[-1].strftime("%Y-%m"), "index": round(last, 1),
            "change_12m_pct": None if (v := _ago(reer, 12)) is None else round(100 * (last / v - 1), 1),
            "vs_10y_average_pct": round(100 * (last / float(ten.mean()) - 1), 1),
        }
    else:
        out["real_exchange_rate"] = None
    gap = _load(iso3, "CREDIT_GAP", metas)
    out["credit_gap"] = None if gap is None or gap.empty else {
        "quarter": _quarter(gap.index[-1]), "pp_of_gdp": round(float(gap.iloc[-1]), 1),
        "above_warning": float(gap.iloc[-1]) > CREDIT_GAP_WARNING,
    }
    houses = _load(iso3, "HOUSE_PRICES", metas)
    out["real_house_prices"] = None if houses is None or len(houses) < 5 else {
        "quarter": _quarter(houses.index[-1]),
        "change_12m_pct": None if (v := _ago(houses, 12)) is None else round(100 * (float(houses.iloc[-1]) / v - 1), 1),
    }
    forecasts = {}
    for key in WEO_KEYS:
        s = _load(iso3, key, metas)
        if s is None:
            continue
        forecasts[key.removeprefix("WEO_").lower()] = {str(y): round(float(s[s.index.year == y].iloc[0]), 1) if (s.index.year == y).any() else None
                                                       for y in (year - 1, year, year + 1)}
    out["imf_forecasts"] = forecasts or None
    out["forecast_years"] = {"last_year": year - 1, "this_year": year, "next_year": year + 1}
    return out, metas


def overview(iso3s: list[str] | None = None, today: date | None = None) -> dict[str, Any]:
    known = [c["iso3"] for c in countries()]
    wanted = [c for c in (iso3s or known) if c in known]
    if not wanted:
        raise MonitorUnavailable("none of those countries is covered")
    rows, metas = [], []
    for iso3 in wanted:
        row, m = snapshot(iso3, today)
        if m:
            rows.append(row)
            metas += m
    if not rows:
        raise MonitorUnavailable("IMF and BIS country data aren't loaded")
    year = (today or date.today()).year
    return {"countries": rows, "forecast_years": {"last_year": year - 1, "this_year": year, "next_year": year + 1},
            "credit_gap_warning_pp": CREDIT_GAP_WARNING, "caveat": CAVEAT,
            "not_loaded": [c for c in wanted if c not in {r["country"] for r in rows}], "sources": metas}
