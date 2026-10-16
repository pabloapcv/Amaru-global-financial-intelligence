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
