#!/usr/bin/env python3
"""
Futures Walk-Forward Validation Demo
=====================================

Demonstrates Amaru's walk-forward forecast evaluation on futures data.

This script:
1. Loads gold, crude oil, and corn continuous futures data
2. Runs walk-forward forecasts against naive baselines
3. Reports whether any model beats the benchmark
4. Shows correlation structure between contracts

Run with: python demos/research/futures_walkforward.py [--live]

By default, uses synthetic fixture data (no API keys needed).
With --live, fetches from Stooq (requires network; free delayed data).

Output: docs/research/futures_walkforward_report.md
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _r(x: float | None, nd: int = 2) -> float | str | None:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def generate_fixture_data(seed: int = 12345) -> dict[str, pd.Series]:
    """Generate synthetic futures data for offline demo."""
    rng = np.random.default_rng(seed)
    end = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
    dates = pd.bdate_range(end - pd.DateOffset(years=8), end)

    base_prices = {"FUT.GC": 1800, "FUT.CL": 75, "FUT.ZC": 450}
    vols = {"FUT.GC": 0.15, "FUT.CL": 0.30, "FUT.ZC": 0.22}
    drifts = {"FUT.GC": 0.0001, "FUT.CL": -0.0001, "FUT.ZC": 0.00005}

    common_factor = rng.normal(0, 0.005, len(dates))

    series = {}
    for sym, base in base_prices.items():
        vol = vols[sym]
        drift = drifts[sym]
        idio = rng.normal(drift, vol / 16, len(dates))
        loading = 0.3 if sym == "FUT.GC" else (0.5 if sym == "FUT.CL" else 0.2)
        returns = idio + loading * common_factor
        prices = base * np.exp(np.cumsum(returns))
        series[sym] = pd.Series(prices, index=dates, name=sym)

    return series


def fetch_live_data() -> dict[str, pd.Series]:
    """Fetch from Stooq (requires network)."""
    from atlas.ingestion.futures import FuturesAdapter

    adapter = FuturesAdapter()
    series = {}
    for sym in ["FUT.GC", "FUT.CL", "FUT.ZC"]:
        result = adapter.fetch(sym)
        points = adapter.parse(sym, result.payload)
        if points:
            dates = pd.DatetimeIndex([p.observed_at for p in points])
            values = [p.value for p in points]
            series[sym] = pd.Series(values, index=dates, name=sym)
    return series


def walk_forward_forecast(
    series: pd.Series,
    horizon_months: int = 3,
    folds: int = 24,
) -> dict[str, Any]:
    """Walk-forward evaluated forecast vs naive baselines."""
    monthly = series.resample("MS").mean().dropna()
    if len(monthly) < 60:
        return {"error": "insufficient data"}

    h = max(1, horizon_months)
    use_log = (monthly > 0).all()
    y = np.log(monthly) if use_log else monthly

    def no_change(tr): return np.repeat(tr.iloc[-1], h)
    def seasonal(tr): return np.array([tr.iloc[-12 + i % 12] for i in range(h)]) if len(tr) >= 12 else no_change(tr)
    def drift(tr):
        n = min(60, len(tr))
        slope = (tr.iloc[-1] - tr.iloc[-n]) / max(n - 1, 1)
        return tr.iloc[-1] + slope * np.arange(1, h + 1)

    methods = {"no_change": no_change, "seasonal_naive": seasonal, "drift": drift}

    min_train = 36
    origins = list(range(max(min_train, len(y) - folds - h), len(y) - h))
    if len(origins) < 6:
        return {"error": f"only {len(y)} observations; need more for walk-forward"}

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

    def score(errs):
        e = np.array([x for x in errs if not math.isnan(x)])
        if not len(e):
            return {"mae": None, "n": 0}
        mae = float(np.mean(np.abs(e))) * (100 if use_log else 1)
        return {"mae": _r(mae, 3), "n": len(e)}

    evaluation = {m: score(e) for m, e in errors.items()}
    valid = {m: v["mae"] for m, v in evaluation.items() if v["mae"] is not None}
    if not valid:
        return {"error": "no valid forecasts"}

    best = min(valid, key=lambda m: valid[m])
    baseline_mae = min(v for m, v in valid.items() if m in ("no_change", "seasonal_naive"))
    beats_baseline = best not in ("no_change", "seasonal_naive") and valid[best] < baseline_mae * 0.98

    return {
        "selected_method": best,
        "beats_naive": beats_baseline,
        "evaluation": evaluation,
        "n_folds": len(origins),
        "data_frequency": "monthly (resampled)",
    }


def compute_correlation(series: dict[str, pd.Series]) -> dict[str, Any]:
    """Compute correlation matrix of returns."""
    monthly = pd.DataFrame({k: v.resample("MS").mean() for k, v in series.items()}).dropna()
    returns = np.log(monthly).diff().dropna()
    if len(returns) < 12:
        return {"error": "insufficient overlapping data"}

    corr = returns.corr()
    names = list(corr.columns)

    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            r = corr.iloc[i, j]
            pairs.append({
                "a": names[i], "b": names[j],
                "correlation": _r(r, 3),
            })

    return {
        "n_observations": len(returns),
        "pairs": pairs,
    }


def generate_report(
    series: dict[str, pd.Series],
    forecasts: dict[str, dict],
    correlation: dict[str, Any],
    data_mode: str,
) -> str:
    """Generate Markdown report."""
    lines = [
        "# Futures Walk-Forward Validation Report",
        "",
        f"*Generated: {date.today().isoformat()} | Data mode: {data_mode}*",
        "",
        "## Overview",
        "",
        "This artifact demonstrates walk-forward forecast evaluation on futures data.",
        "For each contract, we test multiple forecasting methods against naive baselines",
        "and report whether any beats the benchmark out of sample.",
        "",
        "## Data Summary",
        "",
        "| Contract | Observations | Last Price | Last Date |",
        "|----------|--------------|------------|-----------|",
    ]

    for sym, s in series.items():
        lines.append(f"| {sym} | {len(s)} | ${s.iloc[-1]:.2f} | {s.index[-1].date()} |")

    lines.extend([
        "",
        "## Walk-Forward Results",
        "",
        "Each contract is forecasted 3 months ahead using rolling-origin evaluation.",
        "",
        "| Contract | Best Method | Beats Naive? | No-Change MAE | Seasonal MAE | Drift MAE |",
        "|----------|-------------|--------------|---------------|--------------|-----------|",
    ])

    for sym, fc in forecasts.items():
        if "error" in fc:
            lines.append(f"| {sym} | Error: {fc['error']} | — | — | — | — |")
            continue
        ev = fc["evaluation"]
        nc = ev.get("no_change", {}).get("mae", "—")
        sn = ev.get("seasonal_naive", {}).get("mae", "—")
        dr = ev.get("drift", {}).get("mae", "—")
        beats = "**Yes**" if fc["beats_naive"] else "No"
        lines.append(f"| {sym} | {fc['selected_method']} | {beats} | {nc}% | {sn}% | {dr}% |")

    lines.extend([
        "",
        "## Correlation Structure",
        "",
    ])

    if "error" in correlation:
        lines.append(f"Error: {correlation['error']}")
    else:
        lines.append(f"Based on {correlation['n_observations']} monthly return observations:")
        lines.append("")
        lines.append("| Pair | Correlation |")
        lines.append("|------|-------------|")
        for p in correlation["pairs"]:
            lines.append(f"| {p['a']} vs {p['b']} | {p['correlation']} |")

    lines.extend([
        "",
        "## Interpretation",
        "",
        "- **Beats naive = No**: The naive no-change forecast is hard to beat statistically.",
        "  This is a common result in commodity futures and is reported honestly.",
        "- **Correlation**: Positive correlation indicates shared macro sensitivity (e.g., USD, risk appetite).",
        "",
        "## Data Quality Notes",
        "",
        f"- Data mode: **{data_mode}**",
        "- Continuous contracts use synthetic roll (not official exchange methodology)",
        "- Free delayed data has no SLA; gaps may occur around rolls",
        "- For production trading, implement a licensed exchange adapter",
        "",
        "## How to Regenerate",
        "",
        "```bash",
        "# With synthetic data (no network):",
        "python demos/research/futures_walkforward.py",
        "",
        "# With live Stooq data:",
        "python demos/research/futures_walkforward.py --live",
        "```",
    ])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Futures walk-forward demo")
    parser.add_argument("--live", action="store_true", help="Fetch from Stooq (requires network)")
    parser.add_argument("--output", default="docs/research/futures_walkforward_report.md")
    args = parser.parse_args()

    print("Futures Walk-Forward Validation Demo")
    print("=" * 40)

    if args.live:
        print("Mode: LIVE data from Stooq (delayed)")
        try:
            series = fetch_live_data()
            data_mode = "live (Stooq delayed)"
        except Exception as e:
            print(f"Error fetching live data: {e}")
            print("Falling back to synthetic data.")
            series = generate_fixture_data()
            data_mode = "synthetic (fallback from live error)"
    else:
        print("Mode: SYNTHETIC fixture data")
        series = generate_fixture_data()
        data_mode = "synthetic"

    for sym, s in series.items():
        print(f"  {sym}: {len(s)} observations through {s.index[-1].date()}")

    print("\nRunning walk-forward forecasts...")
    forecasts = {}
    for sym, s in series.items():
        fc = walk_forward_forecast(s)
        forecasts[sym] = fc
        if "error" not in fc:
            print(f"  {sym}: best={fc['selected_method']}, beats_naive={fc['beats_naive']}")
        else:
            print(f"  {sym}: {fc['error']}")

    print("\nComputing correlations...")
    correlation = compute_correlation(series)
    if "error" not in correlation:
        print(f"  {correlation['n_observations']} observations, {len(correlation['pairs'])} pairs")

    print("\nGenerating report...")
    report = generate_report(series, forecasts, correlation, data_mode)

    output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report)
    print(f"\nReport written to: {output_path}")


if __name__ == "__main__":
    main()
