"""Fertilizer affordability: how many tons of a crop it takes to buy a ton of fertilizer.

A fertilizer price alone says little to a grower, because crop prices often move with it. Each pair
divides a fertilizer's price by a crop's and ranks the result against the pair's own history, using
the latest month both prices have. Crops are paired with the nutrient they use most.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from atlas.data.access import DataUnavailable, get_series
from atlas.instruments import get_dataset

UREA, DAP, POTASH = "CMD.UREA", "CMD.DAP", "CMD.POTASH"
PAIRS = [("Corn", "CMD.CORN.US", UREA), ("Corn", "CMD.CORN.US", DAP), ("Wheat", "CMD.WHEAT.USHRW", UREA), ("Rice", "CMD.RICE.THAI", UREA),
         ("Soybeans", "CMD.SOYBEANS.US", POTASH), ("Soybeans", "CMD.SOYBEANS.US", DAP), ("Sugar", "CMD.SUGAR.ISA", UREA), ("Coffee", "CMD.COFFEE.ARABICA", UREA)]
CHARTED = [("Corn", UREA), ("Wheat", UREA), ("Soybeans", POTASH)]
FERTILIZER_NAMES = {UREA: "Urea", DAP: "DAP", POTASH: "Potash"}
USD_PER_TON = {"USD per metric ton": 1.0, "US cents per pound": 22.0462}  # 2,204.62 lb a ton, 100 cents a dollar
MIN_MONTHS = 120
HIGH, LOW = 75, 25  # percentiles of the pair's history
CAVEAT = ("World benchmark prices, not what a farm pays: freight, taxes, the local currency and the local crop discount all differ. "
          "The prices are monthly averages, and the crop and fertilizer are not always quoted in the same place. "
          "Several series changed location over time: potash moved to Brazil in 2020 and urea to the Middle East in 2022, "
          "and urea doubles in June 2011 in the source, so older comparisons are rough. A high reading isn't a forecast.")


class FertilizerUnavailable(LookupError):
    pass


def _pair(crop: str, crop_prices: pd.Series, fert_id: str, fert_prices: pd.Series) -> dict[str, Any] | None:
    ratio = (fert_prices / crop_prices).dropna()
    if len(ratio) < MIN_MONTHS:
        return None
    now = float(ratio.iloc[-1])
    earlier = ratio[ratio.index <= ratio.index[-1] - pd.DateOffset(months=12)]
    percentile = round(100 * float((ratio < now).mean()))
    return {
        "crop": crop, "fertilizer": FERTILIZER_NAMES[fert_id], "fertilizer_id": fert_id, "as_of": ratio.index[-1].date().isoformat(),
        "tons_of_crop": round(now, 3), "tons_12m_ago": round(float(earlier.iloc[-1]), 3) if len(earlier) else None,
        "change_12m_pct": round(100 * (now / float(earlier.iloc[-1]) - 1), 1) if len(earlier) else None,
        "median": round(float(ratio.median()), 3), "percentile": percentile, "months": len(ratio), "first_month": ratio.index[0].date().isoformat(),
        "reading": "expensive" if percentile >= HIGH else "cheap" if percentile <= LOW else "typical",
    }


def overview() -> dict[str, Any]:
    """Every pair with enough history, the count by reading and the charted ratios."""
    prices: dict[str, pd.Series] = {}
    metas = {}
    for dataset_id in {d for _, crop_id, fert_id in PAIRS for d in (crop_id, fert_id)}:
        try:
            s, meta = get_series(dataset_id)
        except DataUnavailable:
            continue
        ds = get_dataset(dataset_id)
        assert ds is not None
        s = s.dropna() * USD_PER_TON[ds.unit]
        s.index = s.index.to_period("M").to_timestamp()
        prices[dataset_id], metas[dataset_id] = s, meta
    pairs = []
    for crop, crop_id, fert_id in PAIRS:
        if crop_id in prices and fert_id in prices:
            row = _pair(crop, prices[crop_id], fert_id, prices[fert_id])
            if row:
                pairs.append({**row, "crop_id": crop_id})
    if not pairs:
        raise FertilizerUnavailable("crop and fertilizer prices aren't loaded")
    chart = []
    for crop, fert_id in CHARTED:
        row = next((p for p in pairs if p["crop"] == crop and p["fertilizer_id"] == fert_id), None)
        if row:
            ratio = (prices[fert_id] / prices[row["crop_id"]]).dropna()
            chart.append({"name": f"{crop} per ton of {FERTILIZER_NAMES[fert_id].lower()}",
                          "points": [{"date": d.date().isoformat(), "value": round(float(v), 2)} for d, v in ratio.items()]})
    used = {d for p in pairs for d in (p["crop_id"], p["fertilizer_id"])}
    return {
        "pairs": pairs, "expensive": sum(p["reading"] == "expensive" for p in pairs), "typical": sum(p["reading"] == "typical" for p in pairs),
        "cheap": sum(p["reading"] == "cheap" for p in pairs), "chart": chart, "caveat": CAVEAT, "sources": [metas[d] for d in sorted(used)],
    }
