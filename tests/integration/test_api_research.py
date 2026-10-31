import pytest
from fastapi.testclient import TestClient

from atlas.research import engine


@pytest.fixture(scope="module")
def client(seeded):
    from atlas.api.main import app

    with TestClient(app) as c:
        yield c


def test_instruments_and_quotes(client, no_source):
    inst = client.get("/api/v1/instruments").json()
    assert any(i["dataset_id"] == "CMD.SUGAR.ISA" for i in inst)
    quotes = client.get(f"/api/v1/markets/quotes?ids=CMD.SUGAR.ISA,{no_source}").json()
    sugar, restricted = quotes
    assert sugar["value"] > 0 and sugar["is_tradable_quote"] is False and sugar["observation_date"]
    assert restricted["error"] == "data_unavailable" and restricted["freshness"] == "no_source"


def test_history_unavailable_is_409(client, no_source):
    r = client.get(f"/api/v1/markets/history?id={no_source}")
    assert r.status_code == 409


def test_data_status_shows_freshness_fields(client):
    rows = client.get("/api/v1/data/status").json()
    row = next(r for r in rows if r["dataset_id"] == "FX.USDBRL")
    assert {"observation_date", "ingested_at", "checked_at", "freshness"} <= set(row)


def test_quant_endpoint(client, no_source):
    r = client.post("/api/v1/quant/analyze", json={"analysis": "correlation", "dataset_ids": ["CMD.SUGAR.ISA", "FX.USDBRL"]})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["pairs"][0]["pearson"] is not None
    bad = client.post("/api/v1/quant/analyze", json={"analysis": "forecast", "dataset_ids": [no_source]})
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


@pytest.mark.parametrize("template", ["daily_market_brief", "commodity_deep_dive", "global_macro_outlook", "country_intelligence", "cross_asset"])
def test_report_templates_produce_verified_reports(client, template):
    from atlas.research.templates import TEMPLATES

    t = TEMPLATES[template]
    run_id = engine.create_run(t["default_query"], {"engine": "deterministic", "template": template, "formats": []})
    result = engine.execute_run(run_id)
    assert "error" not in result, result
    assert result["verification"]["passed"], result["verification"]["unverified_numbers"]
    assert result["title"].startswith(t["name"])
    assert client.post("/api/v1/research", json={"query": "x y z", "report": {"template": "nope"}}).status_code == 422


def test_openapi_declares_api_key_auth(client):
    spec = client.get("/openapi.json").json()
    assert "ApiKey" in spec["components"]["securitySchemes"]
    assert {"ApiKey": []} in spec["paths"]["/api/v1/research"]["post"]["security"]


def test_identical_research_is_served_from_cache_until_data_changes(client):
    from atlas.data.db import DatasetState, session_scope, utcnow

    body = {"query": "Copper cache check outlook", "engine": "deterministic", "depth": "quick", "report": {"formats": ["html"]}}
    first = client.post("/api/v1/research", json=body).json()
    import time

    for _ in range(300):  # the API runs research in a background worker
        if engine.get_run(first["run_id"])["status"] in ("complete", "failed"):
            break
        time.sleep(0.1)
    assert engine.get_run(first["run_id"])["status"] == "complete"
    second = client.post("/api/v1/research", json=body).json()
    assert second["cached"] is True and second["status"] == "complete"
    run2 = client.get(f"/api/v1/research/{second['run_id']}").json()
    assert run2["cached_from"] == first["run_id"] and run2["result"]["cached"] is True
    assert client.post("/api/v1/research", json={**body, "no_cache": True}).json()["cached"] is False
    with session_scope() as s:  # new data invalidates the cache
        st = s.get(DatasetState, "CMD.COPPER.LME")
        st.last_changed_at = utcnow()
    assert client.post("/api/v1/research", json=body).json()["cached"] is False


def test_cost_estimate_uses_served_model_prices():
    from types import SimpleNamespace as NS

    from atlas.research.llm import call_cost

    u = NS(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    assert call_cost("claude-opus-5-5", u) == (24.0, True)
    assert call_cost("claude-sonnet-5-5", u) == (12.0, True)
    cost, known = call_cost("claude-unknown", u)
    assert not known and cost == 24.0


def test_follow_up_runs_inherit_subject_and_link_the_thread(seeded):
    from fastapi.testclient import TestClient

    from atlas.api.main import app
    from atlas.research import engine

    c = TestClient(app)
    parent = engine.create_run("Analyze the relationship between the Brazilian real and sugar prices over the last five years.",
                               {"engine": "deterministic", "formats": [], "cache": False})
    engine.execute_run(parent)
    r = c.post("/api/v1/research", json={"query": "What about over ten years?", "engine": "deterministic", "parent_run_id": parent})
    assert r.status_code == 202
    child = r.json()["run_id"]
    import time

    for _ in range(100):
        run = c.get(f"/api/v1/research/{child}").json()
        if run["status"] in ("complete", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "complete", run.get("error")
    assert set(run["plan"]["entities"]) >= {"FX.USDBRL", "CMD.SUGAR.ISA"}  # elliptical: continues the subject
    assert run["plan"]["timeframe"]["lookback_years"] == 10 and "relationship" in run["plan"]["intents"]
    assert run["parent"]["run_id"] == parent
    assert child in [f["run_id"] for f in c.get(f"/api/v1/research/{parent}").json()["follow_ups"]]
    # A follow-up naming a new subject plans on its own.
    own = engine.create_run("How is the US economy doing?", {"engine": "deterministic", "formats": [], "cache": False, "parent_run_id": parent})
    engine.execute_run(own)
    assert "CMD.SUGAR.ISA" not in engine.get_run(own)["plan"]["entities"]

    assert c.post("/api/v1/research", json={"query": "and then?", "parent_run_id": "nope"}).status_code == 404
    queued = engine.create_run("copper outlook", {"engine": "deterministic", "formats": []})
    assert c.post("/api/v1/research", json={"query": "and then?", "parent_run_id": queued}).status_code == 409


def test_scheduler_lease_allows_one_active_scheduler(seeded, monkeypatch):
    from datetime import timedelta

    from sqlalchemy import update

    from atlas.data.db import SchedulerLease, session_scope, utcnow
    from atlas.ingestion import scheduler

    with session_scope() as s:
        s.query(SchedulerLease).delete()
    assert scheduler.acquire("worker-a") and scheduler.acquire("worker-a")  # take, then renew
    assert not scheduler.acquire("worker-b")
    ran = []
    monkeypatch.setattr("atlas.schedules.run_due", lambda: ran.append(1) or [])
    # No network in tests: the iteration's refresh steps are stubbed.
    monkeypatch.setattr("atlas.calendar.refresh_if_due", lambda: None)
    monkeypatch.setattr("atlas.documents.refresh_if_due", lambda: None)
    monkeypatch.setattr(scheduler, "due_datasets", lambda: [])
    assert scheduler.run_once("worker-b") == -1 and ran == []  # the other process does nothing
    with session_scope() as s:  # holder died: lease expires
        s.execute(update(SchedulerLease).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert scheduler.run_once("worker-b") >= 0 and ran == [1]
    st = scheduler.status()
    assert st["active"] and st["holder"] == "worker-b" and st["last_run_at"] and "reports_started" in st["last_result"]
    scheduler.release("worker-b")
    assert not scheduler.status()["active"] and scheduler.acquire("worker-a")
    scheduler.release("worker-a")


def test_readiness_request_ids_and_error_bodies(seeded, monkeypatch):
    from fastapi.testclient import TestClient

    from atlas.api.main import app
    from atlas.data import access

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/health/ready")
    assert r.status_code == 200 and r.json()["ready"] and r.json()["database"] == "ok" and "scheduler" in r.json()
    assert len(r.headers["x-request-id"]) == 32
    assert c.get("/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    assert c.get("/health", headers={"X-Request-ID": "bad id<script>"}).headers["x-request-id"] != "bad id<script>"

    monkeypatch.setattr(access, "data_status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    err = c.get("/api/v1/data/status", headers={"X-Request-ID": "trace-42"})
    assert err.status_code == 500 and err.json()["request_id"] == "trace-42" and "boom" not in err.text
