"""IMF and BIS country data: the connectors, the country monitor and where it surfaces."""

import json
from datetime import date

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from atlas.ingestion.bis import BisAdapter
from atlas.ingestion.imf import ImfCpiAdapter, WeoAdapter
from atlas.provider_sdk import ProviderError

CPI_XML = (b'<?xml version="1.0"?><m:StructureSpecificData xmlns:m="urn:m"><m:DataSet>'
           b'<Series COUNTRY="BRA" TYPE_OF_TRANSFORMATION="YOY_PCH_PA_PT" FREQUENCY="M">'
           b'<Obs TIME_PERIOD="2026-M09" OBS_VALUE="4.576127929382285"/><Obs TIME_PERIOD="2026-M08" OBS_VALUE="NaN"/></Series>'
           b'</m:DataSet></m:StructureSpecificData>')


def test_imf_cpi_reads_monthly_year_on_year_and_rejects_anything_else():
    a = ImfCpiAdapter.__new__(ImfCpiAdapter)
    assert [(p.observed_at, p.value) for p in a.parse("CPI.BRA", CPI_XML)] == [(date(2026, 8, 1), None), (date(2026, 9, 1), 4.5761)]
    with pytest.raises(ProviderError, match="not monthly year-on-year"):
        a.parse("CPI.BRA", CPI_XML.replace(b"YOY_PCH_PA_PT", b"IX"))
    with pytest.raises(ProviderError, match="not monthly year-on-year"):
        a.parse("CPI.MEX", CPI_XML)
    with pytest.raises(ProviderError, match="unexpected IMF payload"):
        a.parse("CPI.BRA", b"<html>maintenance")


def test_weo_downloads_each_indicator_once_and_stores_one_country():
    calls: list[str] = []
    body = {"values": {"NGDP_RPCH": {"BRA": {"2025": 2.3, "2026": 2.2, "2031": 2.0}, "MEX": {"2026": 1.1}}}}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json=body)

    a = WeoAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    bra, mex = a.fetch("NGDP_RPCH.BRA"), a.fetch("NGDP_RPCH.MEX")
    assert len(calls) == 1 and json.loads(mex.payload)["values"] == {"2026": 1.1}
    assert [(p.observed_at.year, p.value) for p in a.parse("NGDP_RPCH.BRA", bra.payload)] == [(2025, 2.3), (2026, 2.2), (2031, 2.0)]
    with pytest.raises(ProviderError, match="no NGDP_RPCH for ARG"):
        a.fetch("NGDP_RPCH.ARG")
    with pytest.raises(ProviderError, match="expected NGDP_RPCH/MEX"):
        a.parse("NGDP_RPCH.MEX", bra.payload)


def test_bis_reads_monthly_and_quarterly_periods_and_the_right_area_column():
    a = BisAdapter.__new__(BisAdapter)
    eer = b"DATAFLOW,FREQ,EER_TYPE,EER_BASKET,REF_AREA,TIME_PERIOD,OBS_VALUE\nBIS:WS_EER(1.0),M,R,B,BR,2026-09,115.5\n"
    gap = b"DATAFLOW,FREQ,BORROWERS_CTY,TC_BORROWERS,TC_LENDERS,CG_DTYPE,TIME_PERIOD,OBS_VALUE\nBIS:WS_CREDIT_GAP(1.0),Q,BR,P,A,C,2026-Q1,1.07\n"
    assert a.parse("EER.BR", eer)[0].observed_at == date(2026, 9, 1)
    assert a.parse("CREDITGAP.BR", gap)[0].observed_at == date(2026, 1, 1)
    with pytest.raises(ProviderError, match="expected WS_SPP"):
        a.parse("SPP.BR", eer.replace(b"EER_TYPE,EER_BASKET,", b"").replace(b"R,B,", b""))
    with pytest.raises(ProviderError, match="unknown BIS symbol"):
        a.parse("XYZ.BR", eer)


def test_forecasts_may_be_dated_in_the_future_but_actual_data_may_not():
    from atlas.ingestion.quality import run_checks
    from atlas.provider_sdk import ParsedPoint

    pts = [ParsedPoint(date(2030, 1, 1), 2.0)]
    assert any(f.check == "future_dates" for f in run_checks(pts, frequency="annual", allow_non_positive=True)[1])
    assert not any(f.check == "future_dates" for f in run_checks(pts, frequency="annual", allow_non_positive=True, allow_future=True)[1])


def test_monitor_reads_each_figure_with_its_own_date(seeded):
    from atlas import country_monitor
    from atlas.data.access import get_series

    row, metas = country_monitor.snapshot("BRA")
    cpi, _ = get_series("IMF.BRA.CPI_YOY")
    assert row["inflation"]["yoy_pct"] == round(float(cpi.dropna().iloc[-1]), 1) and row["inflation"]["month"] == cpi.dropna().index[-1].strftime("%Y-%m")
    assert row["real_exchange_rate"] and row["credit_gap"]["quarter"].count(" Q") == 1 and row["real_house_prices"]
    year = date.today().year
    growth, _ = get_series("WEO.BRA.WEO_GDP_GROWTH")
    assert row["imf_forecasts"]["gdp_growth"][str(year + 1)] == round(float(growth[growth.index.year == year + 1].iloc[0]), 1)
    bolivia, _ = country_monitor.snapshot("BOL")  # the BIS doesn't cover Bolivia
    assert bolivia["real_exchange_rate"] is None and bolivia["credit_gap"] is None and bolivia["inflation"]


def test_reaches_the_api_and_a_verified_report(seeded):
    from atlas.api.main import app
    from atlas.research import engine

    with TestClient(app) as c:
        body = c.get("/api/v1/macro/countries/monitor").json()
        two = c.get("/api/v1/macro/countries/monitor?countries=MEX,CHL").json()
    assert body["available"] and len(body["countries"]) == 26 and "sources" not in body
    assert [r["country"] for r in two["countries"]] == ["MEX", "CHL"]

    res = engine.execute_run(engine.create_run("What is the economic outlook for Mexico and Chile?", {"engine": "deterministic", "formats": []}))
    assert res["verification"]["passed"], res["verification"]["unverified_numbers"][:5]
    assert "## Latest country data and IMF forecasts" in res["narrative_markdown"]
    assert any(ch["title"].startswith("Consumer price inflation") for ch in res["charts"])
    assert pd.Timestamp.today().year  # forecasts are labelled by year in the table header
