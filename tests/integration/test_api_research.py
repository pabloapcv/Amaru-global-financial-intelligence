import pytest
from fastapi.testclient import TestClient

from atlas.research import engine


@pytest.fixture(scope="module")
def client(seeded):
    from atlas.api.main import app

    with TestClient(app) as c:
        yield c


def test_instruments_and_quotes(client):
    inst = client.get("/api/v1/instruments").json()
    assert any(i["dataset_id"] == "CMD.SUGAR.ISA" for i in inst)
    quotes = client.get("/api/v1/markets/quotes?ids=CMD.SUGAR.ISA,CMD.GOLD").json()
    sugar, gold = quotes
    assert sugar["value"] > 0 and sugar["is_tradable_quote"] is False and sugar["observation_date"]
    assert gold["error"] == "data_unavailable" and gold["freshness"] == "no_source"


def test_history_unavailable_is_409(client):
    r = client.get("/api/v1/markets/history?id=CMD.GOLD")
    assert r.status_code == 409


def test_data_status_shows_freshness_fields(client):
    rows = client.get("/api/v1/data/status").json()
    row = next(r for r in rows if r["dataset_id"] == "FX.USDBRL")
    assert {"observation_date", "ingested_at", "checked_at", "freshness"} <= set(row)


def test_quant_endpoint(client):
    r = client.post("/api/v1/quant/analyze", json={"analysis": "correlation", "dataset_ids": ["CMD.SUGAR.ISA", "FX.USDBRL"]})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["pairs"][0]["pearson"] is not None
    bad = client.post("/api/v1/quant/analyze", json={"analysis": "forecast", "dataset_ids": ["CMD.GOLD"]})
    assert bad.status_code == 422 and "DATA UNAVAILABLE" in bad.text


@pytest.mark.parametrize("question", [
    "What's happening with sugar futures today, and what are the main drivers? Outlook next three months.",
    "What happens to copper, gold, and emerging-market currencies if the Federal Reserve cuts interest rates?",
    "How is the US economy performing right now?",
    "Identify unusual price movements across commodities.",
])
def test_deterministic_research_is_fully_verified(client, question):
    run_id = engine.create_run(question, {"engine": "deterministic", "formats": ["html", "xlsx"]})
    result = engine.execute_run(run_id)
    assert "error" not in result, result
    v = result["verification"]
    assert v["passed"], v["unverified_numbers"]
    assert v["numbers_checked"] > 0
    assert not v["invalid_citations"]
    run = client.get(f"/api/v1/research/{run_id}").json()
    assert run["status"] == "complete"

    report_id = result["report_id"]
    html = client.get(f"/api/v1/reports/{report_id}/download?format=html")
    assert html.status_code == 200 and "Appendix A" in html.text
    xlsx = client.get(f"/api/v1/reports/{report_id}/download?format=xlsx")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"


def test_research_endpoint_is_async(client):
    r = client.post("/api/v1/research", json={"query": "copper outlook", "engine": "deterministic", "depth": "quick", "scope": {"lookback": "5Y", "forecast_horizon": "3M"}})
    assert r.status_code == 202
    assert r.json()["status"] == "queued"
    bad = client.post("/api/v1/research", json={"query": "copper", "scope": {"assets": ["NOT.A.THING"]}})
    assert bad.status_code == 422


def test_alerts_lifecycle(client):
    r = client.post("/api/v1/alerts", json={"name": "WTI above 0", "rule": {"type": "threshold", "dataset_id": "CMD.CRUDE.WTI", "op": ">", "value": 0}})
    assert r.status_code == 201
    fired = client.post("/api/v1/alerts/evaluate").json()["fired"]
    assert any("WTI" in f["name"] for f in fired)
    assert client.post("/api/v1/alerts", json={"name": "bad", "rule": {"type": "nope", "dataset_id": "X"}}).status_code == 422


@pytest.mark.parametrize("question", [
    "Compare the economic outlook for Brazil, Mexico, Bolivia, Chile, and Peru.",
    "Is gold positioning crowded?",
    "Bolivia economic outlook",
])
def test_country_and_positioning_research_is_verified(client, question):
    run_id = engine.create_run(question, {"engine": "deterministic", "formats": []})
    result = engine.execute_run(run_id)
    assert "error" not in result, result
    assert result["verification"]["passed"], result["verification"]["unverified_numbers"]
    tools = {e["tool"] for e in result["evidence"]}
    assert tools & {"compare_countries", "get_positioning", "get_country_profile"}


def test_positioning_and_country_endpoints(client):
    r = client.get("/api/v1/positioning?id=CMD.GOLD")  # gold has positioning even without a price source
    assert r.status_code == 200 and r.json()["result"]["dataset_id"] == "COT.GOLD"
    assert client.get("/api/v1/positioning?id=EQ.US.SP500").status_code == 404
    c = client.get("/api/v1/countries/compare?group=latam&indicators=GDP_GROWTH,INFLATION").json()
    assert len(c["result"]["rows"]) == 10


def test_schedule_lifecycle(client):
    r = client.post("/api/v1/schedules", json={"name": "Weekly copper", "query": "copper outlook", "cadence": "weekly", "engine": "deterministic", "depth": "quick", "formats": []})
    assert r.status_code == 201
    sid = r.json()["schedule_id"]
    from atlas import schedules

    run_id = schedules.run_schedule(sid, background=False)
    assert engine.get_run(run_id)["status"] == "complete"
    listed = {s["schedule_id"]: s for s in client.get("/api/v1/schedules").json()}
    assert listed[sid]["last_run_id"] == run_id
    assert client.delete(f"/api/v1/schedules/{sid}").status_code == 204
    assert sid not in {s["schedule_id"] for s in client.get("/api/v1/schedules").json()}


def test_trade_research_and_endpoint(client):
    r = client.get("/api/v1/trade?reporter=BOL&partner=USA").json()["result"]
    assert r["by_year"] and r["products"]["exports"]["top"]
    assert client.get("/api/v1/trade?reporter=BOL&partner=CHN").status_code == 404  # not a loaded pair
    run_id = engine.create_run("Bolivia–U.S. commodity trading opportunities", {"engine": "deterministic", "formats": []})
    result = engine.execute_run(run_id)
    assert result["verification"]["passed"], result["verification"]["unverified_numbers"]
    assert "get_trade_flows" in {e["tool"] for e in result["evidence"]}


def test_regime_quant_endpoint(client):
    r = client.post("/api/v1/quant/analyze", json={"analysis": "regime", "dataset_ids": ["CMD.CRUDE.WTI"]})
    assert r.status_code == 200, r.text
    assert 0 <= r.json()["result"]["markov_high_vol_probability_now"] <= 1


def test_portfolio_research_uses_saved_portfolio(client):
    from atlas import portfolios

    portfolios.save("Energy book", [{"dataset_id": "CMD.CRUDE.BRENT", "weight": 0.6}, {"dataset_id": "CMD.NATGAS.HH", "weight": 0.4}])
    run_id = engine.create_run("What is the value at risk of my portfolio, and how did it do in past stress events?",
                               {"engine": "deterministic", "formats": []})
    result = engine.execute_run(run_id)
    assert result["verification"]["passed"], result["verification"]["unverified_numbers"]
    assert "analyze_portfolio" in {e["tool"] for e in result["evidence"]}
    assert "Portfolio risk" in result["narrative_markdown"]


def test_metrics_endpoint(client, monkeypatch):
    client.get("/api/v1/instruments")
    body = client.get("/metrics").text
    assert 'atlas_http_requests_total{method="GET",route="/api/v1/instruments",status="200"}' in body
    assert "atlas_http_request_duration_seconds_bucket" in body and "atlas_datasets{" in body
    monkeypatch.setenv("ATLAS_METRICS_TOKEN", "s3cret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200
