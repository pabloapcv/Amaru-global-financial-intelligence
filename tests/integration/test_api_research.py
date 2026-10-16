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
