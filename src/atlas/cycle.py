"""Business cycle: a 12-month U.S. recession probability and the past months most like today.

Recession probability: a probit on the 10-year minus 3-month yield spread, the Chicago Fed's
financial conditions index, the 12-month change in unemployment and the Sahm gap. NBER dates arrive
late, so when fitting as of month T only months up to T - horizon - label lag are used. The model
is scored walk-forward: each month's probability comes from a fit that could have been made then.
Two limits are stated in the output: unemployment history is the revised series, not what was
published at the time, and the test covers very few recessions.

The Sahm weight comes out negative: unemployment keeps rising after recessions end, so a high
gap alone often precedes a recession-free year. A simpler model without the labour inputs is scored
beside the main one, so readers can see how much of the record depends on them.

Similar periods: months whose growth, inflation, unemployment, yield-curve and financial-conditions
readings, and their 3-month changes, are closest to today's (Mahalanobis distance), and what markets
did in the following 12 months. This is a descriptive lookup, not a tested signal.
"""

from __future__ import annotations

import logging
import threading
import warnings
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy import stats

from atlas import analytics
from atlas.data.access import DataUnavailable, get_series
from atlas.instruments import get_dataset

log = logging.getLogger("atlas.cycle")

SERIES = {"curve": "US.CURVE.10Y3M", "nfci": "US.FCI.NFCI", "unemployment": "US.UNRATE", "recession": "US.NBER.RECESSION",
          "payrolls": "US.PAYEMS", "production": "US.INDPRO", "core_cpi": "US.CPI.CORE"}
INPUTS = ["curve", "nfci", "unrate_12m", "sahm"]
SIMPLE = ["curve", "nfci"]
INPUT_NAMES = {"curve": "10-year minus 3-month yield spread (pp)", "nfci": "Chicago Fed financial conditions index",
               "unrate_12m": "12-month change in unemployment (pp)", "sahm": "Sahm gap (pp)"}
STATE_NAMES = {"payrolls_12m": "Payrolls, 12-month growth (%)", "production_12m": "Industrial production, 12-month growth (%)",
               "core_cpi_12m": "Core CPI, 12-month growth (%)", "unrate_12m": INPUT_NAMES["unrate_12m"],
               "curve": INPUT_NAMES["curve"], "nfci": INPUT_NAMES["nfci"]}
HORIZON = 12
LABEL_LAG = 12  # NBER dates a recession long after it starts
FIRST_SCORED = "1995-01-01"
MIN_POSITIVE = 6
EXCLUDE_RECENT = 24  # months this close to today are neighbours, not precedents
MIN_GAP = 12  # one episode can't fill every slot
ANALOGS = 5
CAVEAT = ("A statistical estimate for the U.S. only. It has been scored on very few recessions, and unemployment history is the "
          "revised series, so the test may flatter it. Similar periods are a lookup, not a tested signal.")


class CycleUnavailable(ValueError):
    pass


def _series() -> dict[str, pd.Series]:
    out = {}
    for key, dsid in SERIES.items():
        try:
            out[key] = get_series(dsid)[0]
        except DataUnavailable as exc:
            raise CycleUnavailable(f"{dsid} has not been ingested") from exc
    return out


def _months(s: pd.Series) -> pd.Series:
    return s.resample("MS").mean()


def _filled(s: pd.Series) -> pd.Series:
    """Monthly values with a single missing month filled from its neighbours. Without this one gap
    (October 2025 was never published) would blank every 12-month reading that spans it."""
    return _months(s).interpolate(limit=1, limit_area="inside")


def inputs(raw: dict[str, pd.Series]) -> pd.DataFrame:
    """Monthly inputs. Only months with every reading are kept."""
    unemployment = _filled(raw["unemployment"])
    smooth = unemployment.rolling(3).mean()
    return pd.DataFrame({"curve": _months(raw["curve"]), "nfci": _months(raw["nfci"]), "unrate_12m": unemployment.diff(12),
                         "sahm": smooth - smooth.rolling(12).min().shift(1)}).dropna()


def forward_label(recession: pd.Series, horizon: int = HORIZON) -> pd.Series:
    """1 if any of the next `horizon` months is a recession month; unknown until they are all known."""
    ahead = pd.concat([recession.shift(-k) for k in range(1, horizon + 1)], axis=1)
    return ahead.max(axis=1).where(ahead.notna().all(axis=1))


def fit(x: pd.DataFrame, label: pd.Series, columns: list[str], month: pd.Timestamp) -> tuple[pd.Series, int, int] | None:
    """(coefficients, months, recession-ahead months) using only what was knowable at `month`."""
    last = month - pd.DateOffset(months=HORIZON + LABEL_LAG)
    train = x.loc[x.index <= last, columns].join(label.rename("y")).dropna()
    positive = int(train["y"].sum())
    if positive < MIN_POSITIVE or len(train) - positive < MIN_POSITIVE:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = sm.Probit(train["y"], sm.add_constant(train[columns], has_constant="add")).fit(disp=0, maxiter=200)
    except (np.linalg.LinAlgError, ValueError):
        return None
    return (model.params, len(train), positive) if np.isfinite(model.params.values).all() else None


def _probability(params: pd.Series, row: pd.Series, columns: list[str]) -> float:
    return float(stats.norm.cdf(float(params["const"]) + sum(float(params[c]) * float(row[c]) for c in columns)))


def auc(scores: np.ndarray, truth: np.ndarray) -> float | None:
    """Chance that a month before a recession scores above one that isn't (0.5 is a coin flip)."""
    positive, negative = int(truth.sum()), int((1 - truth).sum())
    if not positive or not negative:
        return None
    ranks = stats.rankdata(scores)
    return float((ranks[truth == 1].sum() - positive * (positive + 1) / 2) / (positive * negative))


def walk_forward(x: pd.DataFrame, recession: pd.Series, columns: list[str], first: str = FIRST_SCORED) -> tuple[dict[str, Any], pd.Series]:
    """Refit every month with what was knowable then, and score the months whose outcome is now known."""
    label = forward_label(recession)
    rows = []
    for month in x.index[x.index >= first]:
        fitted = fit(x, label, columns, month)
        if fitted is None:
            continue
        params, n, positive = fitted
        rows.append((month, _probability(params, x.loc[month], columns), positive / n))
    frame = pd.DataFrame(rows, columns=["month", "p", "base"]).set_index("month")
    scored = frame.join(label.rename("y")).dropna()
    if scored.empty:
        raise CycleUnavailable("too little history to score the model")
    y = scored["y"].to_numpy()
    area = auc(scored["p"].to_numpy(), y)
    return {"inputs": columns, "months_scored": len(scored), "months_before_recession": int(y.sum()),
            "first_month": scored.index[0].date().isoformat(), "last_month": scored.index[-1].date().isoformat(),
            "auc": None if area is None else round(area, 3), "brier": round(float(((scored["p"] - y) ** 2).mean()), 4),
            "brier_average_only": round(float(((scored["base"] - y) ** 2).mean()), 4)}, frame["p"]


def _recessions(recession: pd.Series, since: pd.Timestamp) -> int:
    on = recession[recession.index >= since].fillna(0) > 0
    return int((on & ~on.shift(1, fill_value=False)).sum())


def recession_outlook(raw: dict[str, pd.Series]) -> dict[str, Any]:
    x = inputs(raw)
    recession = _months(raw["recession"])
    label = forward_label(recession)
    month = x.index[-1]
    fitted = fit(x, label, INPUTS, month)
    if fitted is None:
        raise CycleUnavailable("too little history to fit the model")
    params, n, positive = fitted
    record, path = walk_forward(x, recession, INPUTS)
    simple, _ = walk_forward(x, recession, SIMPLE)
    probability = _probability(params, x.loc[month], INPUTS)
    # Each input's push on the estimate, in standard deviations of the model's scale.
    pushes = [{"input": c, "name": INPUT_NAMES[c], "reading": round(float(x.loc[month, c]), 2), "weight": round(float(params[c]), 3),
               "push": round(float(params[c]) * float(x.loc[month, c]), 3)} for c in INPUTS]
    first = pd.Timestamp(record["first_month"])
    return {"as_of": month.date().isoformat(), "probability_pct": round(probability * 100, 1), "horizon_months": HORIZON,
            "inputs": pushes, "training_months": n, "training_months_before_recession": positive,
            "record": {**record, "recessions_covered": _recessions(recession, first),
                       "beats_average": record["brier"] < record["brier_average_only"]},
            "simple_model": simple,
            "path": [{"date": t.date().isoformat(), "value": round(float(v) * 100, 1)} for t, v in path.items()],
            "recession_months": [t.date().isoformat() for t, v in recession[recession.index >= first].items() if v > 0]}


def state(raw: dict[str, pd.Series]) -> pd.DataFrame:
    """Monthly readings used to compare periods."""
    x = pd.DataFrame({"payrolls_12m": _filled(raw["payrolls"]).pct_change(12, fill_method=None) * 100,
                      "production_12m": _filled(raw["production"]).pct_change(12, fill_method=None) * 100,
                      "core_cpi_12m": _filled(raw["core_cpi"]).pct_change(12, fill_method=None) * 100,
                      "unrate_12m": _filled(raw["unemployment"]).diff(12),
                      "curve": _months(raw["curve"]), "nfci": _months(raw["nfci"])})
    return x.join(x.diff(3).add_suffix("_3m")).dropna()


def nearest(x: pd.DataFrame, k: int = ANALOGS, exclude_recent: int = EXCLUDE_RECENT, min_gap: int = MIN_GAP) -> list[tuple[pd.Timestamp, float]]:
    """The k months closest to the last one, at least `min_gap` months apart and not recent."""
    now = x.iloc[-1].to_numpy()
    gap = x.to_numpy() - now
    inverse = np.linalg.pinv(np.cov(x.to_numpy(), rowvar=False))
    distance = pd.Series(np.sqrt(np.einsum("ij,jk,ik->i", gap, inverse, gap)), index=x.index)
    cutoff = x.index[-1] - pd.DateOffset(months=exclude_recent)
    out: list[tuple[pd.Timestamp, float]] = []
    for month, d in distance[distance.index <= cutoff].sort_values().items():
        if all(abs((month.year - m.year) * 12 + month.month - m.month) >= min_gap for m, _ in out):
            out.append((month, float(d)))
        if len(out) == k:
            break
    return out


def similar_periods(raw: dict[str, pd.Series], universe: list[str] | None = None) -> dict[str, Any]:
    from atlas import scenarios

    x = state(raw)
    matches = nearest(x)
    label = forward_label(_months(raw["recession"]))
    readings = list(STATE_NAMES)
    windows = [{"start": (m + pd.offsets.MonthEnd(0)).date().isoformat(), "end": (m + pd.offsets.MonthEnd(HORIZON + 1)).date().isoformat()} for m, _ in matches]
    markets = []
    for dsid in universe or scenarios.DEFAULT_UNIVERSE:
        ds = get_dataset(dsid)
        try:
            levels = analytics.monthly_levels(get_series(dsid)[0])
        except DataUnavailable:
            continue
        if ds is None or levels is None:
            continue
        kind = analytics.transform_kind(ds.unit)
        moved = analytics.episode_response(levels, kind, windows)
        markets.append({"dataset_id": dsid, "name": ds.name, "unit": "bp" if kind == "diff" else "pct", **moved})
    return {"as_of": x.index[-1].date().isoformat(), "now": {k: round(float(x[k].iloc[-1]), 2) for k in readings}, "reading_names": STATE_NAMES,
            "periods": [{"month": m.date().isoformat(), "distance": round(d, 2), "readings": {k: round(float(x.loc[m, k]), 2) for k in readings},
                         "recession_within_12m": None if pd.isna(label.get(m)) else bool(label.get(m))} for m, d in matches],
            "markets": markets, "horizon_months": HORIZON}


_lock = threading.Lock()
_cache: dict[str, Any] = {}


def outlook() -> dict[str, Any]:
    """Both views. The walk-forward refit takes a few seconds, so results are kept until the data changes."""
    raw = _series()
    key = "|".join(f"{k}:{s.index[-1]}:{len(s)}" for k, s in raw.items())
    with _lock:
        if _cache.get("key") == key:
            return _cache["value"]
    value = {"recession": recession_outlook(raw), "similar": similar_periods(raw), "caveat": CAVEAT,
             "method": {"recession": "probit on the yield spread, financial conditions, the 12-month change in unemployment and the Sahm gap; "
                                     "fits use only months at least 24 months old, because recession dates arrive late; a single missing month of data is filled from its neighbours",
                        "similar": "Mahalanobis distance on six readings and their 3-month changes; months at least 12 months apart and more than 24 months old"}}
    with _lock:
        _cache.update(key=key, value=value)
    return value


def recession_signal() -> dict[str, Any] | None:
    """The estimate, for the scenario scan, when it is available and has beaten the historical average."""
    try:
        r = outlook()["recession"]
    except (CycleUnavailable, ValueError, KeyError):
        return None
    return r if r["record"]["beats_average"] else None
