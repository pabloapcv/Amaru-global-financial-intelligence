"""Quant engine: deterministic, reproducible financial calculations (spec §6, §11).

These functions never call an LLM. Each result includes a `method` block (what was computed,
on which window, with which transform) and an input `fingerprint`, so the same data
snapshot always reproduces the same numbers.
"""

from __future__ import annotations

import hashlib
import math
import warnings
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

PERIODS_PER_YEAR = {"daily": 252, "weekly": 52, "monthly": 12, "quarterly": 4, "annual": 1}
PANDAS_FREQ = {"daily": "B", "weekly": "W-FRI", "monthly": "MS", "quarterly": "QS", "annual": "YS"}
FREQ_RANK = {"daily": 0, "weekly": 1, "monthly": 2, "quarterly": 3, "annual": 4}
LEVEL_UNITS = ("percent",)
METHOD_NAMES = {"no_change": "no-change", "seasonal_naive": "seasonal-naive", "drift": "drift",
                "damped_trend_ets": "damped-trend exponential smoothing", "arima_110": "ARIMA(1,1,0)"}  # series quoted in percent are analysed in changes, not log returns


def fingerprint(*series: pd.Series) -> str:
    h = hashlib.sha256()
    for s in series:
        h.update(str(s.name).encode())
        h.update(s.index.asi8.tobytes())
        h.update(np.ascontiguousarray(s.to_numpy(dtype=float)).tobytes())
    return h.hexdigest()[:16]


def transform_kind(unit: str) -> str:
    return "diff" if unit.lower() in LEVEL_UNITS else "log_return"


def transform(series: pd.Series, kind: str) -> pd.Series:
    if kind == "diff":
        return series.diff().dropna()
    clean = series[series > 0]
    return np.log(clean).diff().dropna()


def _r(x: float | None, nd: int = 4) -> float | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def resample(series: pd.Series, frequency: str, how: str = "mean") -> pd.Series:
    """Resample to a coarser frequency, labelling periods by their start date."""
    rule = PANDAS_FREQ[frequency]
    if frequency == "daily":
        return series
    grouped = series.resample(rule)
    return (grouped.mean() if how == "mean" else grouped.last()).dropna()


def coarsest(frequencies: list[str]) -> str:
    return max(frequencies, key=lambda f: FREQ_RANK.get(f, 2))


# ---------------------------------------------------------------- performance


def performance(series: pd.Series, *, unit: str, frequency: str) -> dict[str, Any]:
    """Trailing performance, volatility and drawdown on the observed series."""
    kind = transform_kind(unit)
    last_date = series.index[-1]
    last = float(series.iloc[-1])
    horizons = {"1M": pd.DateOffset(months=1), "3M": pd.DateOffset(months=3), "6M": pd.DateOffset(months=6),
                "1Y": pd.DateOffset(years=1), "3Y": pd.DateOffset(years=3), "5Y": pd.DateOffset(years=5)}
    trailing: dict[str, Any] = {}
    for label, off in list(horizons.items()) + [("YTD", None)]:
        anchor_date = pd.Timestamp(year=last_date.year, month=1, day=1) - pd.Timedelta(days=1) if off is None else last_date - off
        past = series[series.index <= anchor_date]
        if past.empty:
            trailing[label] = None
            continue
        base = float(past.iloc[-1])
        entry = {"from_date": past.index[-1].date().isoformat(), "from_value": _r(base, 6)}
        if kind == "diff":
            entry["change_bp"] = _r((last - base) * 100, 1)
        else:
            entry["return_pct"] = _r((last / base - 1) * 100, 2) if base else None
        trailing[label] = entry

    rets = transform(series, kind)
    ppy = PERIODS_PER_YEAR.get(frequency, 12)
    window = rets[rets.index > last_date - pd.DateOffset(years=1)]
    vol = float(window.std(ddof=1) * math.sqrt(ppy)) if len(window) > 5 else None
    ewma = None
    if len(rets) > 20 and kind == "log_return":
        lam = 0.94
        ewma_var = (rets**2).ewm(alpha=1 - lam, adjust=False).mean().iloc[-1]
        ewma = math.sqrt(float(ewma_var) * ppy)

    one_year = series[series.index > last_date - pd.DateOffset(years=1)]
    running_max = series.cummax()
    dd = series / running_max - 1 if kind == "log_return" else series - running_max
    five = dd[dd.index > last_date - pd.DateOffset(years=5)]
    return {
        "last_value": _r(last, 6),
        "last_date": last_date.date().isoformat(),
        "trailing": trailing,
        "high_52w": _r(float(one_year.max()), 6) if len(one_year) else None,
        "low_52w": _r(float(one_year.min()), 6) if len(one_year) else None,
        "percentile_in_5y_range": _r(float(stats.percentileofscore(series[series.index > last_date - pd.DateOffset(years=5)], last)), 1),
        "annualized_vol_1y_pct": _r(vol * 100, 2) if vol is not None and kind == "log_return" else None,
        "annualized_vol_1y_bp": _r(vol * 100, 1) if vol is not None and kind == "diff" else None,
        "ewma_vol_pct": _r(ewma * 100, 2) if ewma else None,
        "max_drawdown_5y_pct": _r(float(five.min()) * 100, 2) if kind == "log_return" and len(five) else None,
        "current_drawdown_pct": _r(float(dd.iloc[-1]) * 100, 2) if kind == "log_return" else None,
        "method": {
            "transform": kind,
            "returns": "simple price return between observation dates" if kind == "log_return" else "level change in basis points",
            "volatility": f"stdev of 1Y {frequency} {'log returns' if kind == 'log_return' else 'changes'} × sqrt({ppy})",
            "ewma": "RiskMetrics EWMA, lambda=0.94" if ewma else None,
            "n_observations": int(len(series)),
        },
        "fingerprint": fingerprint(series),
    }


# ---------------------------------------------------------------- relationships


def align(
    series: dict[str, pd.Series], frequencies: dict[str, str], units: dict[str, str], lookback_years: float | None = None
) -> tuple[pd.DataFrame, str, dict[str, str]]:
    """Put several series on a common (coarsest) frequency and transform to stationary changes.

    Daily series are averaged within the period when the target frequency is coarser,
    which matches the monthly-average construction of the IMF commodity benchmarks.
    """
    freq = coarsest(list(frequencies.values()))
    levels = {}
    for k, s in series.items():
        levels[k] = resample(s, freq, how="mean") if frequencies[k] != freq else s
    df = pd.DataFrame(levels).dropna()
    if lookback_years and not df.empty:
        df = df[df.index > df.index[-1] - pd.DateOffset(months=int(lookback_years * 12))]
    kinds = {k: transform_kind(units[k]) for k in series}
    changes = pd.DataFrame({k: transform(df[k], kinds[k]) for k in df.columns}).dropna()
    return changes, freq, kinds


def correlation(
    series: dict[str, pd.Series], frequencies: dict[str, str], units: dict[str, str], lookback_years: float = 5
) -> dict[str, Any]:
    if len(series) < 2:
        raise ValueError("correlation needs at least two series")
    changes, freq, kinds = align(series, frequencies, units, lookback_years)
    n = len(changes)
    if n < 12:
        raise ValueError(f"only {n} overlapping observations after alignment; too few for a correlation")
    cols = list(changes.columns)
    pairs = []
    window = {"daily": 63, "weekly": 26, "monthly": 12, "quarterly": 8}.get(freq, 12)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = changes[cols[i]], changes[cols[j]]
            r, p = stats.pearsonr(a, b)
            rho, p_s = stats.spearmanr(a, b)
            rolling = a.rolling(window).corr(b).dropna()
            lags = {}
            for lag in (-3, -2, -1, 1, 2, 3):
                shifted = pd.concat([a, b.shift(lag)], axis=1).dropna()
                if len(shifted) > 10:
                    lags[str(lag)] = _r(float(shifted.iloc[:, 0].corr(shifted.iloc[:, 1])), 3)
            # 95% CI via Fisher z
            z = np.arctanh(r)
            se = 1 / math.sqrt(n - 3)
            pairs.append({
                "a": cols[i], "b": cols[j],
                "pearson": _r(r, 3), "pearson_p_value": _r(p, 4),
                "pearson_ci95": [_r(math.tanh(z - 1.96 * se), 3), _r(math.tanh(z + 1.96 * se), 3)],
                "spearman": _r(rho, 3), "spearman_p_value": _r(p_s, 4),
                "rolling_window": window,
                "rolling_latest": _r(float(rolling.iloc[-1]), 3) if len(rolling) else None,
                "rolling_min": _r(float(rolling.min()), 3) if len(rolling) else None,
                "rolling_max": _r(float(rolling.max()), 3) if len(rolling) else None,
                "lead_lag": {"note": f"corr(a_t, b_t+lag) — positive lag means b lags a", **lags},
                "rolling_series": [
                    {"date": d.date().isoformat(), "value": _r(float(v), 3)} for d, v in rolling.items()
                ],
            })
    return {
        "frequency": freq,
        "n_observations": n,
        "start": changes.index[0].date().isoformat(),
        "end": changes.index[-1].date().isoformat(),
        "pairs": pairs,
        "method": {
            "transforms": kinds,
            "alignment": f"resampled to {freq} (period mean) and inner-joined on dates",
            "caveat": "Correlation of changes; it does not establish causation and can be unstable across regimes.",
        },
        "fingerprint": fingerprint(*series.values()),
    }


def regression(
    y_name: str,
    series: dict[str, pd.Series],
    frequencies: dict[str, str],
    units: dict[str, str],
    lookback_years: float = 5,
) -> dict[str, Any]:
    import statsmodels.api as sm

    changes, freq, kinds = align(series, frequencies, units, lookback_years)
    if y_name not in changes:
        raise ValueError(f"dependent variable {y_name} missing after alignment")
    x_cols = [c for c in changes.columns if c != y_name]
    if not x_cols:
        raise ValueError("regression needs at least one explanatory series")
    n = len(changes)
    if n < 12 + len(x_cols):
        raise ValueError(f"only {n} overlapping observations; too few for {len(x_cols)} regressors")
    X = sm.add_constant(changes[x_cols])
    maxlags = {"daily": 5, "weekly": 4, "monthly": 3, "quarterly": 2}.get(freq, 3)
    model = sm.OLS(changes[y_name], X).fit(cov_type="HAC", cov_kwds={"maxlags": maxlags})
    coefs = []
    for name in model.params.index:
        coefs.append({
            "variable": name,
            "coefficient": _r(float(model.params[name]), 5),
            "std_error": _r(float(model.bse[name]), 5),
            "t_stat": _r(float(model.tvalues[name]), 3),
            "p_value": _r(float(model.pvalues[name]), 4),
        })
    return {
        "dependent": y_name,
        "frequency": freq,
        "n_observations": n,
        "start": changes.index[0].date().isoformat(),
        "end": changes.index[-1].date().isoformat(),
        "r_squared": _r(float(model.rsquared), 4),
        "adj_r_squared": _r(float(model.rsquared_adj), 4),
        "coefficients": coefs,
        "residual_std": _r(float(np.sqrt(model.scale)), 6),
        "method": {
            "model": "OLS on period changes",
            "standard_errors": f"Newey-West HAC, maxlags={maxlags}",
            "transforms": kinds,
            "interpretation": "Coefficients are sensitivities of changes (log-return or level change) and are associational, not causal.",
        },
        "fingerprint": fingerprint(*series.values()),
    }


# ---------------------------------------------------------------- anomalies


def anomalies(series: pd.Series, *, unit: str, frequency: str, threshold: float = 3.5, recent: int = 10) -> dict[str, Any]:
    """Robust z-scores of period changes (median/MAD), computed on a trailing window."""
    kind = transform_kind(unit)
    ch = transform(series, kind)
    window = {"daily": 252, "weekly": 104, "monthly": 60, "quarterly": 40}.get(frequency, 60)
    if len(ch) < 30:
        raise ValueError("not enough history for anomaly detection")
    med = ch.rolling(window, min_periods=30).median().shift(1)
    mad = (ch - med).abs().rolling(window, min_periods=30).median().shift(1) * 1.4826
    # Series that rarely move (policy rates) have MAD = 0; fall back to the rolling stdev.
    std = ch.rolling(window, min_periods=30).std().shift(1)
    scale_ = mad.where(mad > 0, std)
    z = ((ch - med) / scale_).where(scale_ > 0, 0.0).dropna()
    flagged = z[z.abs() >= threshold]
    last_z = float(z.iloc[-1]) if len(z) else None
    scale = 100  # log return → %, percentage-point change → bp
    return {
        "latest": {
            "date": z.index[-1].date().isoformat() if len(z) else None,
            "change": _r(float(ch.iloc[-1]) * scale, 3),
            "change_unit": "pct (log)" if kind == "log_return" else "bp",
            "robust_z": _r(last_z, 2),
            "is_anomalous": bool(last_z is not None and abs(last_z) >= threshold),
        },
        "recent_anomalies": [
            {"date": d.date().isoformat(), "robust_z": _r(float(v), 2), "change": _r(float(ch.loc[d]) * scale, 3)}
            for d, v in flagged.iloc[-recent:].items()
        ],
        "anomaly_rate": _r(len(flagged) / len(z), 4) if len(z) else None,
        "method": {
            "statistic": f"robust z = (change − rolling median) / (1.4826 × rolling MAD), window={window}, no look-ahead",
            "threshold": threshold,
            "transform": kind,
        },
        "fingerprint": fingerprint(series),
    }


# ---------------------------------------------------------------- seasonality


def seasonality(series: pd.Series, *, unit: str, frequency: str, years: int = 15) -> dict[str, Any]:
    kind = transform_kind(unit)
    monthly = resample(series, "monthly", "mean") if frequency in ("daily", "weekly") else series
    monthly = monthly[monthly.index > monthly.index[-1] - pd.DateOffset(years=years)]
    ch = transform(monthly, kind)
    by_month = []
    for m in range(1, 13):
        vals = ch[ch.index.month == m]
        if len(vals) < 3:
            continue
        t, p = stats.ttest_1samp(vals, 0.0)
        by_month.append({
            "month": m,
            "mean_change": _r(float(vals.mean()) * 100, 3),
            "hit_rate_up": _r(float((vals > 0).mean()), 3),
            "n": int(len(vals)),
            "p_value": _r(float(p), 4),
        })
    return {
        "by_month": by_month,
        "unit": "pct (log)" if kind == "log_return" else "bp",
        "method": {
            "description": f"Average month-over-month change by calendar month over {years}y; p-values from one-sample t-tests (no multiple-testing correction).",
        },
        "fingerprint": fingerprint(series),
    }


# ---------------------------------------------------------------- forecasting


def _seasonal_naive(train: pd.Series, h: int, season: int) -> np.ndarray:
    if len(train) < season:
        return np.repeat(train.iloc[-1], h)
    last_season = train.iloc[-season:].to_numpy()
    return np.array([last_season[i % season] for i in range(h)])


def _drift(train: pd.Series, h: int) -> np.ndarray:
    n = len(train)
    slope = (train.iloc[-1] - train.iloc[0]) / max(n - 1, 1)
    return train.iloc[-1] + slope * np.arange(1, h + 1)


def _ets(train: pd.Series, h: int) -> np.ndarray:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(train.to_numpy(), trend="add", damped_trend=True).fit()
    return np.asarray(fit.forecast(h))


def _arima(train: pd.Series, h: int) -> np.ndarray:
    from statsmodels.tsa.arima.model import ARIMA

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ARIMA(train.to_numpy(), order=(1, 1, 0)).fit()
    return np.asarray(fit.forecast(h))


def forecast(series: pd.Series, *, unit: str, frequency: str, horizon_months: int = 3, folds: int = 24) -> dict[str, Any]:
    """Walk-forward evaluated forecast, always compared against naive baselines (spec §11)."""
    kind = transform_kind(unit)
    work = resample(series, "monthly", "mean") if frequency in ("daily", "weekly") else series
    work = work[work.index > work.index[-1] - pd.DateOffset(years=20)].dropna()
    h = max(1, horizon_months if frequency in ("daily", "weekly", "monthly") else max(1, horizon_months // 3))
    season = 12 if frequency in ("daily", "weekly", "monthly") else 4
    use_log = kind == "log_return" and (work > 0).all()
    y = np.log(work) if use_log else work

    methods = {
        "no_change": lambda tr: np.repeat(tr.iloc[-1], h),
        "seasonal_naive": lambda tr: _seasonal_naive(tr, h, season),
        "drift": lambda tr: _drift(tr.iloc[-60:], h),
        "damped_trend_ets": lambda tr: _ets(tr.iloc[-120:], h),
        "arima_110": lambda tr: _arima(tr.iloc[-120:], h),
    }
    min_train = 36
    origins = list(range(max(min_train, len(y) - folds - h), len(y) - h))
    if len(origins) < 6:
        raise ValueError(f"only {len(y)} observations; too few for walk-forward evaluation")
    errors: dict[str, list[float]] = {m: [] for m in methods}
    for o in origins:
        train = y.iloc[: o + 1]
        actual = y.iloc[o + h]
        for m, fn in methods.items():
            try:
                pred = fn(train)[h - 1]
                errors[m].append(float(actual - pred))
            except Exception:
                errors[m].append(float("nan"))

    def score(errs: list[float]) -> dict[str, Any]:
        e = np.array([x for x in errs if not math.isnan(x)])
        if not len(e):
            return {"mae": None, "n": 0}
        mae = float(np.mean(np.abs(e)))
        return {"mae": _r(mae * (100 if use_log else 1), 4), "mae_unit": "pct (log)" if use_log else unit, "n": int(len(e))}

    evaluation = {m: score(e) for m, e in errors.items()}
    valid = {m: v["mae"] for m, v in evaluation.items() if v["mae"] is not None}
    best = min(valid, key=lambda m: valid[m])
    baseline_mae = min(v for m, v in valid.items() if m in ("no_change", "seasonal_naive"))
    beats_baseline = best not in ("no_change", "seasonal_naive") and valid[best] < baseline_mae * 0.98

    point_path = methods[best](y)
    resid = np.array([x for x in errors[best] if not math.isnan(x)])
    lo_q, hi_q = (np.quantile(resid, 0.1), np.quantile(resid, 0.9)) if len(resid) >= 8 else (np.nan, np.nan)
    last_date = work.index[-1]
    step = pd.DateOffset(months=1) if season == 12 else pd.DateOffset(months=3)
    path = []
    for i, v in enumerate(point_path):
        d = last_date + step * (i + 1)
        val = float(np.exp(v)) if use_log else float(v)
        entry = {"period": d.date().isoformat(), "point": _r(val, 4)}
        if i == h - 1 and not math.isnan(lo_q):
            entry["p10"] = _r(float(np.exp(v + lo_q)) if use_log else float(v + lo_q), 4)
            entry["p90"] = _r(float(np.exp(v + hi_q)) if use_log else float(v + hi_q), 4)
        path.append(entry)
    return {
        "horizon_periods": h,
        "base_period": last_date.date().isoformat(),
        "base_value": _r(float(work.iloc[-1]), 4),
        "selected_method": best,
        "beats_naive_baseline": bool(beats_baseline),
        "path": path,
        "evaluation": evaluation,
        "method": {
            "evaluation": f"walk-forward, {len(origins)} rolling origins, {h}-step-ahead error, expanding window",
            "selection": "lowest walk-forward MAE among candidates; interval = empirical 10th/90th percentile of that method's walk-forward errors",
            "data": f"{'monthly averages of ' if frequency in ('daily', 'weekly') else ''}{'log ' if use_log else ''}levels",
            "assumptions": "No structural break; past error distribution representative of the horizon; no fundamental inputs.",
            "disclaimer": "Statistical extrapolation for research context only — not a trading signal."
            + ("" if beats_baseline else " The selected model does NOT beat the naive baseline, so treat the path as 'no-change' guidance."),
        },
        "fingerprint": fingerprint(series),
    }


# ---------------------------------------------------------------- scenarios


def scenario(
    asset: str,
    driver: str,
    shock: float,
    series: dict[str, pd.Series],
    frequencies: dict[str, str],
    units: dict[str, str],
    lookback_years: float = 10,
) -> dict[str, Any]:
    """Hypothetical: apply a shock to a driver and translate it through the historical beta.

    `shock` is in the driver's change units: percent for price-like series (e.g. +5 = +5%),
    basis points for percent-unit series (e.g. -50 = a 50bp cut).
    """
    reg = regression(asset, {asset: series[asset], driver: series[driver]}, frequencies, units, lookback_years)
    coef = next(c for c in reg["coefficients"] if c["variable"] == driver)
    kinds = reg["method"]["transforms"]
    # % shock → log-return units; bp shock → percentage points
    x = math.log1p(shock / 100.0) if kinds[driver] == "log_return" else shock / 100.0
    beta, se = coef["coefficient"], coef["std_error"]
    impact = beta * x
    lo, hi = (beta - 1.96 * se) * x, (beta + 1.96 * se) * x
    to_out = (lambda v: (math.exp(v) - 1) * 100) if kinds[asset] == "log_return" else (lambda v: v * 100)
    return {
        "label": "HYPOTHETICAL SCENARIO",
        "asset": asset,
        "driver": driver,
        "shock": shock,
        "shock_unit": "pct" if kinds[driver] == "log_return" else "bp",
        "implied_asset_move": _r(to_out(impact), 3),
        "implied_asset_move_ci95": sorted([_r(to_out(lo), 3), _r(to_out(hi), 3)]),
        "implied_unit": "pct" if kinds[asset] == "log_return" else "bp",
        "beta": beta,
        "beta_p_value": coef["p_value"],
        "r_squared": reg["r_squared"],
        "n_observations": reg["n_observations"],
        "frequency": reg["frequency"],
        "method": {
            "model": "single-factor historical sensitivity (HAC OLS on period changes)",
            "assumptions": "Linear, stable relationship; all else equal; same-period response. Ignores other drivers and regime shifts.",
            "caveat": "Hypothetical illustration, not a forecast." + (" Beta is not statistically significant at 5%." if coef["p_value"] is None or coef["p_value"] > 0.05 else ""),
        },
        "fingerprint": reg["fingerprint"],
    }


# ---------------------------------------------------------------- positioning


def positioning_stats(df: pd.DataFrame, lookback_years: float = 3) -> dict[str, Any]:
    """Speculative positioning summary from a COT frame (see data.access.get_positioning).

    The COT index places the latest net non-commercial position within its trailing range
    (0 = most net-short, 100 = most net-long in the window).
    """
    last_date = df.index[-1]
    window = df[df.index > last_date - pd.DateOffset(months=int(lookback_years * 12))]
    last, prev = df.iloc[-1], df.iloc[-2] if len(df) > 1 else df.iloc[-1]
    net = window["noncomm_net"]
    lo, hi = float(net.min()), float(net.max())
    cot_index = (float(last["noncomm_net"]) - lo) / (hi - lo) * 100 if hi > lo else None
    std = float(net.std(ddof=1)) if len(net) > 2 else 0.0
    z = (float(last["noncomm_net"]) - float(net.mean())) / std if std > 0 else None
    oi = float(last["open_interest"])
    four_weeks = df[df.index <= last_date - pd.Timedelta(days=28)]
    return {
        "report_date": last_date.date().isoformat(),
        "open_interest": _r(oi, 0),
        "open_interest_change_1w": _r(oi - float(prev["open_interest"]), 0),
        "noncomm_net": _r(float(last["noncomm_net"]), 0),
        "noncomm_net_change_1w": _r(float(last["noncomm_net"] - prev["noncomm_net"]), 0),
        "noncomm_net_change_4w": _r(float(last["noncomm_net"] - four_weeks["noncomm_net"].iloc[-1]), 0) if len(four_weeks) else None,
        "noncomm_net_pct_oi": _r(float(last["noncomm_net"]) / oi * 100, 2) if oi else None,
        "comm_net": _r(float(last["comm_net"]), 0),
        "comm_net_pct_oi": _r(float(last["comm_net"]) / oi * 100, 2) if oi else None,
        "cot_index": _r(cot_index, 1),
        "net_zscore": _r(z, 2),
        "window_net_min": _r(lo, 0),
        "window_net_max": _r(hi, 0),
        "positioning_extreme": None if cot_index is None else ("crowded long" if cot_index >= 90 else "crowded short" if cot_index <= 10 else None),
        "method": {
            "net": "non-commercial long − non-commercial short (contracts); legacy futures-only report",
            "cot_index": f"position of latest net within its {lookback_years:g}-year min–max range, 0–100",
            "zscore": f"(latest net − {lookback_years:g}y mean) / {lookback_years:g}y stdev",
            "caveat": "Positions are as of the Tuesday report date and published the following Friday. Crowded positioning is a risk condition, not a timing signal.",
            "n_reports_in_window": int(len(window)),
        },
        "fingerprint": fingerprint(df["noncomm_net"].rename("noncomm_net"), df["open_interest"].rename("open_interest")),
    }


# ---------------------------------------------------------------- volatility regimes


def volatility_regime(series: pd.Series, *, unit: str, frequency: str, years: int = 10) -> dict[str, Any]:
    """Classify the current volatility regime two ways.

    1. Realized-volatility percentile: current annualized volatility (last ~3 months of
       period changes) ranked against its own rolling history.
    2. Two-state Markov-switching model (switching variance) fitted to period changes,
       giving the probability that the market is in its high-volatility state now.
    """
    import statsmodels.api as sm

    kind = transform_kind(unit)
    work = series[series.index > series.index[-1] - pd.DateOffset(years=years)]
    if frequency == "daily":
        work = work.resample("W-FRI").last().dropna()
        freq_used, ppy, window = "weekly", 52, 13
    else:
        freq_used, ppy, window = frequency, PERIODS_PER_YEAR.get(frequency, 12), {"monthly": 6, "quarterly": 4}.get(frequency, 6)
    ch = transform(work, kind).dropna()
    if len(ch) < 60:
        raise ValueError(f"only {len(ch)} {freq_used} observations; need at least 60 for regime analysis")
    scale = 100.0  # % for log returns, bp-equivalent percentage points ×100 for level changes
    rv = ch.rolling(window).std(ddof=1).dropna() * math.sqrt(ppy) * scale
    current_rv = float(rv.iloc[-1])
    pct = float(stats.percentileofscore(rv, current_rv))
    bucket = "low" if pct < 25 else "normal" if pct < 75 else "elevated" if pct < 95 else "stress"

    y = (ch - ch.mean()) * scale
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = sm.tsa.MarkovRegression(y.to_numpy(), k_regimes=2, trend="n", switching_variance=True)
        fit = model.fit(disp=False, search_reps=5)
    sig = np.sqrt(np.asarray(fit.params[-2:]))  # sigma2 for regimes 0, 1
    high = int(np.argmax(sig))
    probs = fit.smoothed_marginal_probabilities[:, high]
    p_trans = fit.regime_transition
    stay_high = float(np.asarray(p_trans)[high, high, 0]) if np.ndim(p_trans) == 3 else None
    in_high = probs > 0.5
    episodes, start = [], None
    for i, flag in enumerate(in_high):
        if flag and start is None:
            start = i
        if (not flag or i == len(in_high) - 1) and start is not None:
            end = i if flag else i - 1
            if end - start + 1 >= 3:
                episodes.append({"start": ch.index[start].date().isoformat(), "end": ch.index[end].date().isoformat(), "periods": end - start + 1})
            start = None
    return {
        "frequency_used": freq_used,
        "realized_vol_annualized": _r(current_rv, 2),
        "realized_vol_unit": "pct" if kind == "log_return" else "bp",
        "realized_vol_percentile": _r(pct, 1),
        "realized_vol_regime": bucket,
        "markov_high_vol_probability_now": _r(float(probs[-1]), 3),
        "markov_regime_vol_annualized": {
            "low": _r(float(sig[1 - high]) * math.sqrt(ppy), 2),
            "high": _r(float(sig[high]) * math.sqrt(ppy), 2),
        },
        "markov_high_regime_persistence": _r(stay_high, 3),
        "expected_high_regime_duration_periods": _r(1 / (1 - stay_high), 1) if stay_high is not None and stay_high < 1 else None,
        "share_of_time_in_high_regime": _r(float(in_high.mean()), 3),
        "recent_high_vol_episodes": episodes[-5:],
        "probability_series": [{"date": d.date().isoformat(), "value": _r(float(p), 3)} for d, p in zip(ch.index, probs)],
        "method": {
            "realized": f"rolling {window}-period stdev of {freq_used} changes, annualized; percentile vs the {years}-year history",
            "markov": "2-state Markov-switching model with regime-specific variance (Hamilton filter, smoothed probabilities)",
            "caveat": "Smoothed probabilities use the full sample, so past regime dates are in-sample; the latest probability is the actionable reading.",
            "n_observations": int(len(ch)),
        },
        "fingerprint": fingerprint(series),
    }


# ---------------------------------------------------------------- portfolio risk

STRESS_WINDOWS = [
    {"name": "Global financial crisis", "start": "2008-09-01", "end": "2008-12-31"},
    {"name": "COVID crash", "start": "2020-02-14", "end": "2020-04-30"},
    {"name": "2022 energy shock", "start": "2022-02-18", "end": "2022-06-30"},
    {"name": "2022 rate shock (H2)", "start": "2022-07-01", "end": "2022-10-31"},
]


def portfolio_risk(
    series: dict[str, pd.Series],
    weights: dict[str, float],
    frequencies: dict[str, str],
    factors: dict[str, pd.Series] | None = None,
    factor_units: dict[str, str] | None = None,
    factor_frequencies: dict[str, str] | None = None,
    lookback_years: float = 5,
    full_history: dict[str, pd.Series] | None = None,
) -> dict[str, Any]:
    """Risk of a long/short portfolio of price-like instruments.

    Weights are fractions of capital (negative = short). Returns are simple returns at the
    coarsest frequency among holdings, so monthly benchmarks and daily prices are comparable.
    """
    ids = list(weights)
    freq = coarsest([frequencies[i] for i in ids])
    ppy = PERIODS_PER_YEAR.get(freq, 12)
    levels = pd.DataFrame({i: resample(series[i], freq, "mean") if frequencies[i] != freq else series[i] for i in ids}).dropna()
    levels = levels[levels.index > levels.index[-1] - pd.DateOffset(months=int(lookback_years * 12))]
    rets = levels.pct_change().dropna()
    if len(rets) < 24:
        raise ValueError(f"only {len(rets)} overlapping {freq} returns; need at least 24")
    w = np.array([weights[i] for i in ids], dtype=float)
    port = rets.to_numpy() @ w
    port_s = pd.Series(port, index=rets.index)
    cov = rets.cov().to_numpy() * ppy
    vol = float(np.sqrt(w @ cov @ w))
    marginal = cov @ w / vol if vol > 0 else np.zeros_like(w)
    contrib = w * marginal
    asset_vol = np.sqrt(np.diag(cov))
    standalone = float(np.sum(np.abs(w) * asset_vol))

    def var_cvar(level: float) -> tuple[float, float]:
        q = float(np.quantile(port, 1 - level))
        tail = port[port <= q]
        return -q, -float(tail.mean()) if len(tail) else -q

    v95, c95 = var_cvar(0.95)
    v99, c99 = var_cvar(0.99)
    z95 = 1.6448536269514722
    mu, sd = float(np.mean(port)), float(np.std(port, ddof=1))
    wealth = (1 + port_s).cumprod()
    dd = wealth / wealth.cummax() - 1

    holdings = []
    for k, i in enumerate(ids):
        holdings.append({
            "dataset_id": i, "weight": _r(float(w[k]), 4),
            "annualized_vol_pct": _r(float(asset_vol[k]) * 100, 2),
            "risk_contribution_pct": _r(float(contrib[k] / vol * 100), 2) if vol > 0 else None,
            "marginal_vol_pct": _r(float(marginal[k]) * 100, 3),
        })
    corr = rets.corr()

    exposures = None
    if factors:
        fa_changes, _, kinds = align(
            {**factors, "__port__": wealth}, {**(factor_frequencies or {}), "__port__": freq},
            {**(factor_units or {}), "__port__": "index"}, lookback_years,
        )
        if len(fa_changes) >= 24:
            import statsmodels.api as sm

            X = sm.add_constant(fa_changes[list(factors)])
            fit = sm.OLS(fa_changes["__port__"], X).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
            exposures = {
                "frequency": coarsest([freq, *(factor_frequencies or {}).values()]),
                "r_squared": _r(float(fit.rsquared), 4),
                "betas": [
                    {"factor": f, "beta": _r(float(fit.params[f]), 4), "p_value": _r(float(fit.pvalues[f]), 4),
                     "factor_transform": kinds[f]}
                    for f in factors
                ],
                "n_observations": int(len(fa_changes)),
                "interpretation": "Beta on log-return factors = % portfolio move per 1% factor move; on percent-unit factors (yields) = portfolio log-return per 1 percentage point change.",
            }

    stresses = []
    hist = full_history or series
    for wnd in STRESS_WINDOWS:
        start, end = pd.Timestamp(wnd["start"]), pd.Timestamp(wnd["end"])
        moves, missing = {}, []
        for i in ids:
            s = hist[i]
            before, during = s[s.index <= start], s[(s.index > start) & (s.index <= end)]
            if before.empty or during.empty:
                missing.append(i)
                continue
            moves[i] = float(during.iloc[-1] / before.iloc[-1] - 1)
        covered = sum(abs(weights[i]) for i in moves) / max(sum(abs(v) for v in weights.values()), 1e-12)
        stresses.append({
            **wnd,
            "portfolio_return_pct": _r(sum(weights[i] * m for i, m in moves.items()) * 100, 2) if moves else None,
            "holding_returns_pct": {i: _r(m * 100, 2) for i, m in moves.items()},
            "missing_holdings": missing,
            "weight_coverage_pct": _r(covered * 100, 1),
        })

    return {
        "frequency": freq,
        "n_observations": int(len(rets)),
        "start": rets.index[0].date().isoformat(),
        "end": rets.index[-1].date().isoformat(),
        "gross_exposure": _r(float(np.sum(np.abs(w))), 4),
        "net_exposure": _r(float(np.sum(w)), 4),
        "annualized_vol_pct": _r(vol * 100, 2),
        "diversification_ratio": _r(standalone / vol, 3) if vol > 0 else None,
        "annualized_return_pct": _r(((1 + port_s).prod() ** (ppy / len(port_s)) - 1) * 100, 2),
        "max_drawdown_pct": _r(float(dd.min()) * 100, 2),
        "var": {
            "horizon": f"one {freq.removesuffix('ly') if freq != 'daily' else 'day'}",
            "historical_95_pct": _r(v95 * 100, 2), "historical_99_pct": _r(v99 * 100, 2),
            "cvar_95_pct": _r(c95 * 100, 2), "cvar_99_pct": _r(c99 * 100, 2),
            "parametric_95_pct": _r((-(mu - z95 * sd)) * 100, 2),
        },
        "holdings": holdings,
        "correlation": {"labels": ids, "matrix": [[_r(float(x), 3) for x in row] for row in corr.to_numpy()]},
        "factor_exposures": exposures,
        "stress_tests": stresses,
        "cumulative": [{"date": d.date().isoformat(), "value": _r(float(v), 5)} for d, v in wealth.items()],
        "method": {
            "returns": f"simple {freq} returns of aligned levels (period means for daily series when mixing frequencies)",
            "volatility": "sqrt(w' Σ w), Σ annualized sample covariance",
            "risk_contribution": "w_i (Σw)_i / σ_p, summing to 100%",
            "var": "historical quantile of portfolio period returns; CVaR = mean beyond VaR; parametric = normal",
            "stress": "price change of each holding over the historical window applied to current weights (no rebalancing)",
            "caveat": "Benchmark/reference prices, not tradable quotes; no transaction costs, carry or roll yield.",
        },
        "fingerprint": fingerprint(*[series[i] for i in ids]),
    }
