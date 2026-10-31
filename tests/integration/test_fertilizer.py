"""Fertilizer affordability: the ratio, its reading and where it surfaces."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient


def monthly(values: list[float], end: str = "2026-07-01") -> pd.Series:
    return pd.Series(values, index=pd.date_range(end=end, periods=len(values), freq="MS"), dtype=float)


def test_ratio_uses_the_latest_shared_month_and_ranks_it():
    from atlas.fertilizer import _pair

    crop = monthly([100.0] * 200)
    fert = monthly([100.0 + i for i in range(199)] + [400.0, 999.0], end="2026-08-01")  # a month past the crop
    row = _pair("Corn", crop, "CMD.UREA", fert)
    assert row["as_of"] == "2026-07-01" and row["tons_of_crop"] == 4.0
    assert row["tons_12m_ago"] == pytest.approx(2.87) and row["change_12m_pct"] == pytest.approx(39.4)
    assert row["percentile"] == 100 and row["reading"] == "expensive"


def test_readings_and_short_history():
    from atlas.fertilizer import _pair

    crop = monthly([100.0] * 200)
    assert _pair("Corn", crop, "CMD.UREA", monthly([300.0 - i for i in range(200)]))["reading"] == "cheap"
    assert _pair("Corn", crop, "CMD.UREA", monthly([100.0 + (i % 2) * 2 for i in range(199)] + [101.0]))["reading"] == "typical"
    assert _pair("Corn", crop, "CMD.UREA", monthly([100.0] * 60)) is None


def test_cents_per_pound_become_dollars_per_ton(seeded):
    from atlas import fertilizer
    from atlas.data.access import get_series

    sugar = next(p for p in fertilizer.overview()["pairs"] if p["crop"] == "Sugar")
    urea, _ = get_series("CMD.UREA")
    cents, _ = get_series("CMD.SUGAR.ISA")
    month = pd.Timestamp(sugar["as_of"])
    expected = float(urea[urea.index.to_period("M") == month.to_period("M")].iloc[0]) / (float(cents[cents.index.to_period("M") == month.to_period("M")].iloc[0]) * 22.0462)
    assert sugar["tons_of_crop"] == pytest.approx(expected, abs=0.001)


def test_unavailable_without_prices(monkeypatch):
    from atlas import fertilizer
    from atlas.data.access import DataUnavailable

    def missing(_):
        raise DataUnavailable("no data")

    monkeypatch.setattr(fertilizer, "get_series", missing)
    with pytest.raises(fertilizer.FertilizerUnavailable):
        fertilizer.overview()


def test_reaches_the_api_and_a_verified_report(seeded):
    from atlas.api.main import app
    from atlas.research import engine
    from atlas.research.planner import build_plan

    with TestClient(app) as c:
        body = c.get("/api/v1/macro/fertilizer").json()
    assert body["available"] and len(body["pairs"]) == 8 and len(body["chart"]) == 3 and "sources" not in body
    assert body["expensive"] + body["typical"] + body["cheap"] == 8

    assert any(s["tool"] == "get_fertilizer_affordability" for s in build_plan("Is fertilizer affordable for corn growers?")["steps"])
    assert not any(s["tool"] == "get_fertilizer_affordability" for s in build_plan("What is happening with gold?")["steps"])

    res = engine.execute_run(engine.create_run("Are urea and potash expensive for farmers?", {"engine": "deterministic", "formats": []}))
    assert res["verification"]["passed"], res["verification"]["unverified_numbers"][:5]
    assert "## Fertilizer affordability" in res["narrative_markdown"]
    assert any(ch["title"].startswith("Tons of crop") for ch in res["charts"])
