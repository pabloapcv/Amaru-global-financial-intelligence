"""CFTC and World Bank parsing, positioning statistics and schedule cadence."""

import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from atlas import analytics
from atlas.ingestion.cftc import CftcAdapter
from atlas.ingestion.worldbank import WorldBankAdapter
from atlas.provider_sdk import ProviderError
from atlas.schedules import next_run


def test_cftc_parse_computes_net_speculative_position():
    rows = [{
        "report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000", "open_interest_all": "1158944",
        "noncomm_positions_long_all": "406286", "noncomm_positions_short_all": "189011",
        "noncomm_postions_spread_all": "198079", "comm_positions_long_all": "448754",
        "comm_positions_short_all": "726098", "nonrept_positions_long_all": "105825",
        "nonrept_positions_short_all": "45756",
    }, {"report_date_as_yyyy_mm_dd": "2026-09-29T00:00:00.000"}]  # incomplete row is skipped
    pts = CftcAdapter.parse(CftcAdapter.__new__(CftcAdapter), "080732", json.dumps(rows).encode())
    assert len(pts) == 1
    assert pts[0].value == 406286 - 189011
    assert pts[0].fields["comm_short"] == 726098


def test_cftc_parse_rejects_error_payload():
    with pytest.raises(ProviderError):
        CftcAdapter.parse(CftcAdapter.__new__(CftcAdapter), "080732", b'{"error": "bad query"}')


def test_worldbank_parse_keeps_missing_years_as_missing():
    payload = json.dumps({"country": "BOL", "indicator": "FP.CPI.TOTL.ZG",
                          "rows": [{"date": "2024", "value": 5.1}, {"date": "2023", "value": None}]}).encode()
    pts = WorldBankAdapter.parse(WorldBankAdapter.__new__(WorldBankAdapter), "BOL:FP.CPI.TOTL.ZG", payload)
    assert {p.observed_at.year: p.value for p in pts} == {2024: 5.1, 2023: None}
    with pytest.raises(ProviderError):
        WorldBankAdapter.parse(WorldBankAdapter.__new__(WorldBankAdapter), "BRA:FP.CPI.TOTL.ZG", payload)


def test_positioning_stats_cot_index_and_crowding():
    idx = pd.date_range("2023-01-03", periods=160, freq="W-TUE")
    net = np.linspace(-100, 100, 160)
    df = pd.DataFrame({"noncomm_net": net, "comm_net": -net, "open_interest": 1000.0}, index=idx)
    st = analytics.positioning_stats(df, lookback_years=3)
    assert st["cot_index"] == 100.0
    assert st["positioning_extreme"] == "crowded long"
    assert st["noncomm_net_pct_oi"] == 10.0


@pytest.mark.parametrize("cadence,expected", [
    ("daily", datetime(2026, 10, 17, 11)),
    ("weekly", datetime(2026, 10, 19, 11)),   # next Monday
    ("monthly", datetime(2026, 11, 1, 11)),
])
def test_schedule_cadence(cadence, expected):
    assert next_run(cadence, datetime(2026, 10, 16, 15, 0)) == expected


def test_comtrade_parse_keeps_flow_and_chapter_dimensions():
    from atlas.ingestion.comtrade import ComtradeAdapter

    payload = json.dumps({"reporter": "BOL", "partner": "USA", "rows": [
        {"year": 2025, "flow": "X", "cmd": "TOTAL", "value": 420144236.0},
        {"year": 2025, "flow": "X", "cmd": "80", "value": 101459522.0},
        {"year": 2025, "flow": "M", "cmd": "TOTAL", "value": None},
    ]}).encode()
    pts = ComtradeAdapter.parse(ComtradeAdapter.__new__(ComtradeAdapter), "BOL:USA", payload)
    assert [dict(p.dims) for p in pts] == [{"flow": "X", "cmd": "TOTAL"}, {"flow": "X", "cmd": "80"}, {"flow": "M", "cmd": "TOTAL"}]
    assert pts[2].value is None
    with pytest.raises(ProviderError):
        ComtradeAdapter.parse(ComtradeAdapter.__new__(ComtradeAdapter), "BOL:CHN", payload)


def test_quality_dedupes_on_dimensions_not_just_dates():
    from datetime import date

    from atlas.ingestion import quality
    from atlas.provider_sdk import ParsedPoint

    d = date(2025, 1, 1)
    pts = [ParsedPoint(d, 1.0, dims=(("flow", "X"), ("cmd", "TOTAL"))),
           ParsedPoint(d, 2.0, dims=(("flow", "M"), ("cmd", "TOTAL"))),
           ParsedPoint(d, 3.0, dims=(("flow", "M"), ("cmd", "TOTAL")))]
    clean, findings, _ = quality.run_checks(pts, frequency="annual", allow_non_positive=False)
    assert len(clean) == 2 and any(f.check == "duplicates" for f in findings)


def test_volatility_regime_detects_high_vol_state():
    rng = np.random.default_rng(7)
    calm, wild = rng.normal(0, 0.005, 900), rng.normal(0, 0.03, 100)
    s = pd.Series(100 * np.exp(np.cumsum(np.concatenate([calm, wild]))), index=pd.bdate_range("2019-01-01", periods=1000))
    r = analytics.volatility_regime(s, unit="usd", frequency="daily")
    assert r["markov_high_vol_probability_now"] > 0.9
    assert r["realized_vol_regime"] in ("elevated", "stress")
    assert r["markov_regime_vol_annualized"]["high"] > 2 * r["markov_regime_vol_annualized"]["low"]


def test_agent_validation():
    from atlas.agents import AgentError, validate

    ok_action = {"template": "research_report", "query": "why?"}
    validate(["CMD.SUGAR.ISA"], [{"type": "anomaly", "z": 3}], ok_action)
    with pytest.raises(AgentError, match="market instrument"):
        validate(["US.CPI.HEADLINE"], [{"type": "anomaly"}], ok_action)
    with pytest.raises(AgentError, match="trigger type"):
        validate(["CMD.SUGAR.ISA"], [{"type": "moon_phase"}], ok_action)
    with pytest.raises(AgentError, match="template"):
        validate(["CMD.SUGAR.ISA"], [{"type": "anomaly"}], {"template": "nope", "query": "why?"})
