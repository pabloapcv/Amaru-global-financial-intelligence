"""EIA energy inventories: the connector, the seasonal comparison and where it surfaces."""

import json
from datetime import date

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from atlas.ingestion.eia import DEMO_KEY, EiaAdapter
from atlas.provider_sdk import ProviderError


def payload(rows, total=None) -> bytes:
    return json.dumps({"response": {"total": len(rows) if total is None else total, "data": rows}}).encode()


def adapter(body: bytes, status: int = 200, key: str | None = None) -> tuple[EiaAdapter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, content=body)

    return EiaAdapter(httpx.Client(transport=httpx.MockTransport(handler)), api_key=key), seen


def test_parses_a_series_and_keeps_the_key_out_of_the_url():
    rows = [{"period": "2026-10-23", "series": "WCESTUS1", "units": "MBBL", "value": 410625},
            {"period": "2026-10-16", "series": "WCESTUS1", "units": "MBBL", "value": "409500"}]
    a, seen = adapter(payload(rows), key="secret-key")
    fetched = a.fetch("PET.WCESTUS1.W")
    assert "secret-key" not in fetched.request and seen[0].headers["X-Api-Key"] == "secret-key"
    points = a.parse("PET.WCESTUS1.W", fetched.payload)
    assert [(p.observed_at, p.value) for p in points] == [(date(2026, 10, 16), 409500.0), (date(2026, 10, 23), 410625.0)]


def test_falls_back_to_the_demo_key_and_explains_its_limit(monkeypatch):
    monkeypatch.setattr("atlas.provider_sdk.time.sleep", lambda _: None)
    a, seen = adapter(b"{}", status=429)
    with pytest.raises(ProviderError, match="EIA_API_KEY"):
        a.fetch("PET.WCESTUS1.W")
    assert seen[0].headers["X-Api-Key"] == DEMO_KEY


def test_a_long_retry_after_fails_now_instead_of_waiting():
    from atlas.provider_sdk import with_retries

    waits: list[float] = []
    with pytest.raises(ProviderError, match="retry in 15528s"):
        with_retries(lambda: httpx.Response(429, headers={"retry-after": "15528"}), sleep=waits.append)
    assert waits == []


def test_eia_series_refresh_once_a_day():
    from atlas.ingestion.pipeline import REFRESH_INTERVAL
    from atlas.instruments import datasets

    eia = [d for d in datasets().values() if d.provider_id == "eia"]
    assert len(eia) == 8 and {REFRESH_INTERVAL[d.meta["refresh"]].days for d in eia} == {1}


@pytest.mark.parametrize("body,match", [
    (payload([{"period": "2026-10-23", "series": "WCESTUS1", "units": "BBL", "value": 1}]), "unit"),
    (payload([{"period": "2026-10-23", "series": "WGTSTUS1", "units": "MBBL", "value": 1}]), "expected WCESTUS1"),
    (payload([], total=10), "0 of 10"),
    (b'{"error": "invalid api_key"}', "unexpected EIA payload"),
])
def test_fails_loudly_on_a_changed_or_partial_payload(body, match):
    with pytest.raises(ProviderError, match=match):
        EiaAdapter.__new__(EiaAdapter).parse("PET.WCESTUS1.W", body)


def test_compares_with_the_same_week_of_the_previous_five_years():
    from atlas.energy import _stock, seasonal

    dates = pd.date_range(end="2026-10-23", periods=6 * 52 + 2, freq="W-FRI")
    s = pd.Series(100_000.0, index=dates)
    for k, v in zip(range(1, 6), (90_000, 95_000, 100_000, 105_000, 110_000)):
        target = pd.Timestamp("2026-10-23") - pd.DateOffset(years=k)
        s.iloc[s.index.get_indexer([target], method="nearest")[0]] = v
    s.iloc[-1] = 120_000.0
    assert seasonal(s, s.index[-1]) == (100_000.0, 90_000.0, 110_000.0)
    row = _stock("Crude oil (excluding SPR)", "US.EIA.CRUDE_STOCKS", "million barrels", 1000.0, s)
    assert row["level"] == 120.0 and row["weekly_change"] == 20.0 and row["five_year_average"] == 100.0
    assert row["vs_five_year_average_pct"] == 20.0 and row["position"] == "above the five-year range"
    assert _stock("x", "x", "x", 1.0, s.iloc[-100:]) is None  # under five years of history


def test_covers_energy_but_not_vegetable_oils():
    from atlas.energy import covers

    assert covers("CMD.CRUDE.WTI") == "oil" and covers("Crude oil (WTI)") == "oil" and covers("natural gas") == "gas"
    assert covers("palm oil") is None and covers("soybean oil") is None and covers("gold") is None


def test_reaches_the_api_fundamentals_and_a_verified_report(seeded):
    from atlas.api.main import app
    from atlas.research import engine
    from atlas.research.planner import build_plan

    with TestClient(app) as c:
        body = c.get("/api/v1/macro/energy").json()
        gas = c.get("/api/v1/macro/energy?kind=gas").json()
        fund = c.get("/api/v1/fundamentals?id=CMD.CRUDE.WTI")
    assert body["available"] and len(body["stocks"]) == 4 and len(body["flows"]) == 3 and body["spr"] and "sources" not in body
    assert [s["name"] for s in gas["stocks"]] == ["Natural gas in storage"] and gas["flows"] == [] and gas["spr"] is None
    assert fund.status_code == 404  # the Markets fundamentals panel stays USDA-only

    steps = build_plan("Are U.S. crude oil inventories high?")["steps"]
    assert [s["tool"] for s in steps].count("get_energy_inventories") == 1 and not any(s["tool"] == "get_fundamentals" for s in steps)
    assert not any(s["tool"] == "get_energy_inventories" for s in build_plan("Are sugar inventories tight?")["steps"])

    res = engine.execute_run(engine.create_run("Is the oil market oversupplied? Look at U.S. inventories.", {"engine": "deterministic", "formats": []}))
    assert res["verification"]["passed"], res["verification"]["unverified_numbers"][:5]
    assert "## U.S. energy inventories" in res["narrative_markdown"]
    assert any(ch["title"].startswith("U.S. stocks against") for ch in res["charts"])
