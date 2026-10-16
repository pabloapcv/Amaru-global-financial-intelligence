import numpy as np
import pandas as pd
import pytest

from atlas import analytics


def _monthly(values, start="2010-01-01", name="x"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="MS"), name=name)


def test_correlation_recovers_known_relationship():
    rng = np.random.default_rng(0)
    x = rng.normal(0, 0.03, 200)
    y = 0.8 * x + rng.normal(0, 0.01, 200)
    a = _monthly(100 * np.exp(np.cumsum(x)), name="A")
    b = _monthly(100 * np.exp(np.cumsum(y)), name="B")
    res = analytics.correlation({"A": a, "B": b}, {"A": "monthly", "B": "monthly"}, {"A": "usd", "B": "usd"}, lookback_years=20)
    pair = res["pairs"][0]
    assert pair["pearson"] > 0.85
    assert pair["pearson_ci95"][0] < pair["pearson"] < pair["pearson_ci95"][1]
    assert pair["pearson_p_value"] < 0.001


def test_regression_recovers_beta():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 0.03, 240)
    y = 1.5 * x + rng.normal(0, 0.01, 240)
    a = _monthly(100 * np.exp(np.cumsum(y)), name="Y")
    b = _monthly(100 * np.exp(np.cumsum(x)), name="X")
    res = analytics.regression("Y", {"Y": a, "X": b}, {"Y": "monthly", "X": "monthly"}, {"Y": "usd", "X": "usd"}, lookback_years=25)
    beta = next(c for c in res["coefficients"] if c["variable"] == "X")
    assert beta["coefficient"] == pytest.approx(1.5, abs=0.1)
    assert res["r_squared"] > 0.9


def test_mixed_frequency_alignment_uses_coarsest():
    daily = pd.Series(np.linspace(100, 200, 2000), index=pd.bdate_range("2015-01-01", periods=2000), name="D")
    monthly = analytics.resample(daily, "monthly", "mean")
    changes, freq, _ = analytics.align({"D": daily, "M": monthly}, {"D": "daily", "M": "monthly"}, {"D": "usd", "M": "usd"})
    assert freq == "monthly"
    assert (changes["D"] - changes["M"]).abs().max() < 1e-12


def test_percent_unit_series_use_level_changes():
    s = _monthly([1.0, 1.5, 1.25])
    out = analytics.transform(s, analytics.transform_kind("percent"))
    assert list(out.round(6)) == [0.5, -0.25]


def test_anomaly_detects_injected_spike_without_lookahead():
    rng = np.random.default_rng(2)
    r = rng.normal(0, 0.01, 600)
    r[-1] = 0.12
    s = pd.Series(100 * np.exp(np.cumsum(r)), index=pd.bdate_range("2020-01-01", periods=600))
    res = analytics.anomalies(s, unit="usd", frequency="daily")
    assert res["latest"]["is_anomalous"]
    assert res["latest"]["robust_z"] > 8


def test_anomaly_handles_flat_series():
    s = pd.Series([5.0] * 300 + [5.25], index=pd.bdate_range("2020-01-01", periods=301))
    res = analytics.anomalies(s, unit="percent", frequency="daily")
    assert res["latest"]["date"] == s.index[-1].date().isoformat()


def test_forecast_reports_baselines_and_walk_forward():
    rng = np.random.default_rng(3)
    s = _monthly(100 * np.exp(np.cumsum(rng.normal(0, 0.04, 180))))
    res = analytics.forecast(s, unit="usd", frequency="monthly", horizon_months=3)
    assert {"no_change", "seasonal_naive"} <= set(res["evaluation"])
    assert res["horizon_periods"] == 3 and len(res["path"]) == 3
    assert "walk-forward" in res["method"]["evaluation"]
    # A random walk should not be beaten by much; the flag must be a real bool either way.
    assert isinstance(res["beats_naive_baseline"], bool)
    if not res["beats_naive_baseline"]:
        assert "does NOT beat" in res["method"]["disclaimer"]


def test_scenario_sign_and_units():
    rng = np.random.default_rng(4)
    x = rng.normal(0, 0.1, 200)  # yield changes in percentage points
    y = -0.05 * x + rng.normal(0, 0.005, 200)  # asset falls when yields rise
    rates = _monthly(3 + np.cumsum(x), name="R")
    asset = _monthly(100 * np.exp(np.cumsum(y)), name="A")
    res = analytics.scenario("A", "R", -50, {"A": asset, "R": rates}, {"A": "monthly", "R": "monthly"}, {"A": "usd", "R": "percent"}, lookback_years=20)
    assert res["shock_unit"] == "bp" and res["implied_unit"] == "pct"
    assert res["implied_asset_move"] == pytest.approx(2.5, abs=0.5)  # -0.5pp × -5%/pp ≈ +2.5%
    assert res["label"] == "HYPOTHETICAL SCENARIO"


def test_fingerprint_is_deterministic():
    s = _monthly([1.0, 2.0, 3.0])
    assert analytics.fingerprint(s) == analytics.fingerprint(s.copy())
    assert analytics.fingerprint(s) != analytics.fingerprint(s * 1.0001)
