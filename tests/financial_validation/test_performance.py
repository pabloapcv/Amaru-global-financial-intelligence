"""Hand-checked financial calculations: returns, drawdowns and basis-point changes."""

import pandas as pd
import pytest

from atlas import analytics


def test_trailing_returns_and_drawdown_match_hand_calculation():
    idx = pd.date_range("2024-01-01", periods=25, freq="MS")
    values = [100.0] * 12 + [120.0, 90.0] + [110.0] * 11  # peak 120, trough 90
    s = pd.Series(values, index=idx)
    p = analytics.performance(s, unit="USD per metric ton", frequency="monthly")
    assert p["last_value"] == 110.0
    assert p["trailing"]["1Y"]["return_pct"] == pytest.approx((110 / 120 - 1) * 100, abs=0.01)
    assert p["trailing"]["1M"]["return_pct"] == 0.0
    assert p["max_drawdown_5y_pct"] == pytest.approx(-25.0, abs=0.01)
    assert p["current_drawdown_pct"] == pytest.approx((110 / 120 - 1) * 100, abs=0.01)


def test_yield_changes_are_reported_in_basis_points():
    idx = pd.bdate_range("2025-01-01", periods=300)
    s = pd.Series([4.0] * 299 + [4.25], index=idx)
    p = analytics.performance(s, unit="percent", frequency="daily")
    assert p["trailing"]["1M"]["change_bp"] == pytest.approx(25.0)
    assert "return_pct" not in p["trailing"]["1M"]
    assert p["current_drawdown_pct"] is None
