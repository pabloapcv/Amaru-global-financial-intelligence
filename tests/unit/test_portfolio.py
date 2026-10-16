import numpy as np
import pandas as pd
import pytest

from atlas import analytics


def _prices(seed, n=120, vol=0.05, drift=0.0):
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.exp(np.cumsum(rng.normal(drift, vol, n))), index=pd.date_range("2016-01-01", periods=n, freq="MS"))


def test_risk_contributions_sum_to_100_and_var_is_ordered():
    s = {"A": _prices(1), "B": _prices(2), "C": _prices(3, vol=0.02)}
    r = analytics.portfolio_risk(s, {"A": 0.5, "B": 0.3, "C": -0.2}, {k: "monthly" for k in s}, lookback_years=10)
    assert sum(h["risk_contribution_pct"] for h in r["holdings"]) == pytest.approx(100, abs=0.05)
    v = r["var"]
    assert 0 < v["historical_95_pct"] <= v["historical_99_pct"] and v["historical_95_pct"] <= v["cvar_95_pct"]
    assert r["gross_exposure"] == 1.0 and r["net_exposure"] == 0.6


def test_perfectly_correlated_assets_have_no_diversification():
    a = _prices(5)
    r = analytics.portfolio_risk({"A": a, "B": a.copy() * 2}, {"A": 0.5, "B": 0.5}, {"A": "monthly", "B": "monthly"}, lookback_years=10)
    assert r["diversification_ratio"] == pytest.approx(1.0, abs=1e-3)


def test_stress_window_uses_current_weights_and_reports_coverage():
    idx = pd.date_range("2019-01-01", "2021-12-01", freq="MS")
    a = pd.Series(100.0, index=idx)
    a[(a.index > "2020-02-14") & (a.index <= "2020-04-30")] = 80.0  # -20% through the COVID window
    b = pd.Series(np.linspace(100, 110, len(idx)), index=idx)
    r = analytics.portfolio_risk({"A": a, "B": b}, {"A": 1.0, "B": 0.0001}, {"A": "monthly", "B": "monthly"}, lookback_years=5)
    covid = next(s for s in r["stress_tests"] if s["name"] == "COVID crash")
    assert covid["holding_returns_pct"]["A"] == -20.0
    gfc = next(s for s in r["stress_tests"] if s["name"] == "Global financial crisis")
    assert gfc["portfolio_return_pct"] is None and set(gfc["missing_holdings"]) == {"A", "B"}


def test_holdings_validation():
    from atlas.portfolios import PortfolioError, validate_holdings

    with pytest.raises(PortfolioError, match="not a price"):
        validate_holdings([{"dataset_id": "RATE.UST.10Y", "weight": 0.5}])
    with pytest.raises(PortfolioError, match="no price source"):
        validate_holdings([{"dataset_id": "CMD.GOLD", "weight": 0.5}])
    with pytest.raises(PortfolioError, match="twice"):
        validate_holdings([{"dataset_id": "CMD.CRUDE.WTI", "weight": 0.5}, {"dataset_id": "CMD.CRUDE.WTI", "weight": 0.2}])
