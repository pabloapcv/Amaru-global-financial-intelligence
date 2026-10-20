import csv
import io
import zipfile
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas import analytics
from atlas.data.access import get_fundamentals, get_series, series_meta
from atlas.ingestion import pipeline
from atlas.ingestion.psd import PsdAdapter
from atlas.instruments import fundamentals_for, get_dataset

PSD_HEADER = ["Commodity_Code", "Commodity_Description", "Country_Code", "Country_Name", "Market_Year", "Calendar_Year",
              "Month", "Attribute_ID", "Attribute_Description", "Unit_ID", "Unit_Description", "Value"]


def _zip(rows: list[list[str]]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(PSD_HEADER)
    w.writerows(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("psd_grains_pulses.csv", "﻿" + out.getvalue())
    return buf.getvalue()


def _row(com: str, country: str, my: int, attr: str, value: float, month: str = "10") -> list[str]:
    return ["0440000", com, country[:2].upper(), country, str(my), "2026", month, "028", attr, "08", "(1000 MT)", f"{value:.4f}"]


def test_adapter_filters_commodity_attributes_and_years():
    payload = _zip([
        _row("Corn", "Brazil", 2025, "Production", 130000), _row("Corn", "Brazil", 2025, "Yield", 5.5),
        _row("Corn", "Brazil", 1985, "Production", 20000), _row("Wheat", "Brazil", 2025, "Production", 8000),
        _row("Corn", "Brazil", 2025, "Domestic Consumption", 90000),
    ])
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, content=payload)

    adapter = PsdAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    fetched = adapter.fetch("psd_corn")
    points = adapter.parse("psd_corn", fetched.payload)
    got = {(p.observed_at.year, dict(p.dims)["attribute"]): p.value for p in points}
    assert got == {(2025, "production"): 130000.0, (2025, "consumption"): 90000.0}  # no yield, no 1985, no wheat
    assert points[0].fields == {"release": 202610}
    adapter.fetch("psd_wheat")  # corn and wheat share one group download per run
    assert len(calls) == 1 and calls[0].endswith("psd_grains_pulses_csv.zip")


def test_pipeline_publishes_and_tracks_usda_revisions(seeded):
    from atlas.ingestion.psd import COLUMNS

    ds = get_dataset("FUND.CORN")
    meta = series_meta("FUND.CORN")
    assert meta.freshness == "current" and meta.n_observations == 25
    df, _ = get_fundamentals("FUND.CORN")
    latest = int(df["market_year"].max())
    before = float(df[(df.market_year == latest) & (df.country_name == "Brazil") & (df.attribute == "production")].value.iloc[0])

    payload = ",".join(COLUMNS) + f"\n{latest},BR,Brazil,production,{before - 5000:.1f},{latest}-10\n"
    fake = type(seeded)(overrides={"psd_corn": payload.encode()})
    res = pipeline.ingest_dataset(ds, adapter=fake)
    assert res.status == "success" and res.rows_revised == 1 and res.rows_inserted == 0
    df, _ = get_fundamentals("FUND.CORN")
    row = df[(df.market_year == latest) & (df.country_name == "Brazil") & (df.attribute == "production")].iloc[0]
    assert row.value == pytest.approx(before - 5000) and row.prev_value == pytest.approx(before) and row.release == latest * 100 + 10

    res = analytics.fundamentals_balance(df, unit=ds.unit)
    rev = next(v for v in res["revisions_vs_previous_report"] if v["attribute"] == "production")
    assert rev["change"] == pytest.approx(-5000, abs=0.1)
    assert res["status"][res["latest_market_year"]] == "USDA projection"
    assert [t["country"] for t in res["top_producers"]][0] == "Brazil"
    world = res["balance"][-1]
    assert world["stocks_to_use_pct"] == pytest.approx(world["ending_stocks"] / world["consumption"] * 100, abs=0.01)
    # World production is the primary series.
    s, _ = get_series("FUND.CORN")
    assert s.index[-1].year == latest and s.iloc[-1] == pytest.approx(world["production"], abs=0.1)


def test_fundamentals_resolution_and_coverage():
    assert fundamentals_for("CMD.SUGAR.ISA") == "FUND.SUGAR"
    assert fundamentals_for("Soybeans (US No.2 yellow)") == "FUND.SOYBEANS"
    assert fundamentals_for("how tight is the cotton market") == "FUND.COTTON"
    assert fundamentals_for("Crude oil (Brent, Europe spot)") is None
    assert fundamentals_for("Copper (grade A cathode)") is None


def test_tool_report_and_api(seeded):
    from atlas.research import engine, tools

    ctx = tools.ToolContext()
    out = tools.TOOLS["get_fundamentals"]["fn"](ctx, {"commodity": "corn", "geography": "India"})
    assert out["geography"] == "India" and "top_producers" not in out and "price_vs_stocks_to_use" not in out
    world = tools.TOOLS["get_fundamentals"]["fn"](ctx, {"commodity": "CMD.CORN.US"})
    pv = world["price_vs_stocks_to_use"]
    assert pv["stocks_basis"] == "world excluding China" and pv["n_years"] >= 12 and "implied_price" in pv
    gap = tools.TOOLS["get_fundamentals"]["fn"](ctx, {"commodity": "copper"})
    assert gap["status"] == "unavailable" and "USDA PSD covers" in gap["reason"]
    with pytest.raises(tools.ToolError):
        tools.TOOLS["get_fundamentals"]["fn"](ctx, {"commodity": "corn", "geography": "Atlantis"})

    rid = engine.create_run("Is the corn market tight? Supply and stocks?", {"engine": "deterministic", "formats": [], "cache": False})
    res = engine.execute_run(rid)
    assert "## Supply and demand (USDA)" in res["narrative_markdown"]
    assert res["verification"]["numbers_verified"] == res["verification"]["numbers_checked"]
    assert res["verification"]["invalid_citations"] == []

    from atlas.api.main import app

    c = TestClient(app)
    r = c.get("/api/v1/fundamentals", params={"id": "CMD.CORN.US"})
    assert r.status_code == 200 and r.json()["chart"]["series"][0]["name"] == "Production"
    assert c.get("/api/v1/fundamentals", params={"id": "CMD.CRUDE.BRENT"}).status_code == 404
    assert c.get("/api/v1/fundamentals", params={"id": "CMD.CORN.US", "country": "Atlantis"}).status_code == 409
    assert date.fromisoformat(r.json()["result"]["balance"][-1]["market_year"][:4] + "-01-01").year == date.today().year


def test_usda_revision_agent_fires_once_per_report(seeded):
    from atlas import agents
    from atlas.ingestion.psd import COLUMNS

    a = agents.create("Corn USDA watch", ["CMD.CORN.US"], [{"type": "usda_revision", "abs_pct": 1.0}],
                      {"query": "What changed in USDA's corn balance?", "engine": "deterministic"}, 1, False, "local", "local")
    df, _ = get_fundamentals("FUND.CORN")
    latest = int(df["market_year"].max())
    cur = float(df[(df.market_year == latest) & (df.country_name == "Brazil") & (df.attribute == "production")].value.iloc[0])
    payload = ",".join(COLUMNS) + f"\n{latest},BR,Brazil,production,{cur - 9000:.1f},{latest}-11\n"
    pipeline.ingest_dataset(get_dataset("FUND.CORN"), adapter=type(seeded)(overrides={"psd_corn": payload.encode()}))

    r = agents.check_agent(a["agent_id"], background=False)
    assert r["fired"], r
    f = r["findings"][0]
    assert f["trigger"] == "usda_revision" and f["dedupe_key"] == f"FUND.CORN:{latest}-11" and f["value"] < -1
    assert "world production" in f["message"]
    # Same report, checked again after the cooldown (forced): no second report.
    assert agents.check_agent(a["agent_id"], background=False, force=True)["fired"] is False
