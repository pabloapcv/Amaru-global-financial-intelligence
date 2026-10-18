"""Accounts, sessions, API keys, quotas and workspace isolation (spec §15, §16)."""

import pytest
from fastapi.testclient import TestClient

from atlas import auth


@pytest.fixture()
def secured(seeded, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTH", "required")
    from atlas.api.main import app

    with TestClient(app) as c:
        yield c


def _signup(client, email, workspace):
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": "correct horse battery", "name": email.split("@")[0], "workspace_name": workspace})
    assert r.status_code == 201, r.text
    return r.json()


def test_password_hashing_roundtrip():
    h = auth.hash_password("correct horse battery")
    assert h.startswith("scrypt$") and "correct" not in h
    assert auth.verify_password("correct horse battery", h)
    assert not auth.verify_password("wrong password!!", h)


def test_requests_without_credentials_are_rejected(secured):
    secured.cookies.clear()
    assert secured.get("/api/v1/instruments").status_code == 401
    assert secured.get("/health").status_code == 200  # health stays public


def test_signup_login_session_cookie_and_logout(secured):
    _signup(secured, "ana@example.test", "Ana's desk")
    assert secured.get("/api/v1/account").json()["user"]["email"] == "ana@example.test"
    secured.post("/api/v1/auth/logout")
    assert secured.get("/api/v1/account").status_code == 401
    bad = secured.post("/api/v1/auth/login", json={"email": "ana@example.test", "password": "nope nope nope"})
    assert bad.status_code == 401
    ok = secured.post("/api/v1/auth/login", json={"email": "ana@example.test", "password": "correct horse battery"})
    assert ok.status_code == 200 and secured.get("/api/v1/account").status_code == 200


def test_signup_validation(secured):
    assert secured.post("/api/v1/auth/signup", json={"email": "not-an-email", "password": "correct horse battery"}).status_code == 422
    assert secured.post("/api/v1/auth/signup", json={"email": "short@example.test", "password": "short"}).status_code == 422


def test_workspaces_cannot_see_each_others_research(secured):
    from atlas.research import engine

    a = _signup(secured, "alice@example.test", "Alice")
    run_a = engine.create_run("copper outlook", {"engine": "deterministic", "depth": "quick", "formats": ["html"]},
                              a["user"]["user_id"], a["workspace"]["workspace_id"])
    result = engine.execute_run(run_a)
    report_id = result["report_id"]
    assert secured.get(f"/api/v1/research/{run_a}").status_code == 200
    assert secured.get(f"/api/v1/reports/{report_id}/download").status_code == 200

    secured.cookies.clear()
    _signup(secured, "bob@example.test", "Bob")
    assert secured.get(f"/api/v1/research/{run_a}").status_code == 404
    assert secured.get(f"/api/v1/reports/{report_id}").status_code == 404
    assert secured.get(f"/api/v1/reports/{report_id}/download").status_code == 404
    assert run_a not in {r["run_id"] for r in secured.get("/api/v1/research").json()}
    assert report_id not in {r["report_id"] for r in secured.get("/api/v1/reports").json()}


def test_alerts_and_schedules_are_workspace_scoped(secured):
    _signup(secured, "carol@example.test", "Carol")
    alert = secured.post("/api/v1/alerts", json={"name": "WTI > 0", "rule": {"type": "threshold", "dataset_id": "CMD.CRUDE.WTI", "op": ">", "value": 0}}).json()
    sched = secured.post("/api/v1/schedules", json={"name": "weekly", "query": "copper outlook", "cadence": "weekly"}).json()
    secured.cookies.clear()
    _signup(secured, "dave@example.test", "Dave")
    assert alert["alert_id"] not in {a["alert_id"] for a in secured.get("/api/v1/alerts").json()}
    assert secured.delete(f"/api/v1/alerts/{alert['alert_id']}").status_code == 404
    assert secured.post(f"/api/v1/schedules/{sched['schedule_id']}/run").status_code == 404
    assert secured.delete(f"/api/v1/schedules/{sched['schedule_id']}").status_code == 404


def test_api_keys_authenticate_and_can_be_revoked(secured):
    _signup(secured, "erin@example.test", "Erin")
    key = secured.post("/api/v1/api-keys", json={"name": "notebook"}).json()
    assert key["key"].startswith("atk_")
    assert key["key"] not in str(secured.get("/api/v1/api-keys").json())  # shown once, never listed
    secured.cookies.clear()
    headers = {"Authorization": f"Bearer {key['key']}"}
    assert secured.get("/api/v1/account", headers=headers).json()["auth"] == "api_key"
    assert secured.post("/api/v1/api-keys", json={"name": "x"}, headers=headers).status_code == 403
    secured.post("/api/v1/auth/login", json={"email": "erin@example.test", "password": "correct horse battery"})
    assert secured.delete(f"/api/v1/api-keys/{key['key_id']}").status_code == 204
    secured.cookies.clear()
    assert secured.get("/api/v1/account", headers=headers).status_code == 401


def test_plan_quotas_return_429(secured):
    _signup(secured, "frank@example.test", "Frank")
    limit = auth.PLANS["explorer"]["schedules"]
    for i in range(limit):
        assert secured.post("/api/v1/schedules", json={"name": f"s{i}", "query": "copper outlook", "cadence": "weekly"}).status_code == 201
    r = secured.post("/api/v1/schedules", json={"name": "over", "query": "copper outlook", "cadence": "weekly"})
    assert r.status_code == 429 and "plan limit" in r.json()["detail"]


def test_data_refresh_requires_operator(secured):
    _signup(secured, "gina@example.test", "Gina")
    assert secured.post("/api/v1/pipelines/run", json={}).status_code == 403


def test_login_throttling(secured):
    for _ in range(auth.MAX_ATTEMPTS):
        secured.post("/api/v1/auth/login", json={"email": "victim@example.test", "password": "guess guess guess"})
    r = secured.post("/api/v1/auth/login", json={"email": "victim@example.test", "password": "guess guess guess"})
    assert r.status_code == 401 and "too many attempts" in r.json()["detail"]


def test_portfolios_are_workspace_scoped_and_analyzable(secured):
    _signup(secured, "hank@example.test", "Hank")
    body = {"name": "Commodity tilt", "holdings": [{"dataset_id": "CMD.CRUDE.WTI", "weight": 0.5}, {"dataset_id": "CMD.COPPER.LME", "weight": 0.3}, {"dataset_id": "FX.USDBRL", "weight": -0.2}]}
    pid = secured.post("/api/v1/portfolios", json=body).json()["portfolio_id"]
    risk = secured.get(f"/api/v1/portfolios/{pid}/risk").json()["risk"]
    assert risk["annualized_vol_pct"] > 0 and len(risk["holdings"]) == 3
    bad = secured.post("/api/v1/portfolios", json={"name": "x", "holdings": [{"dataset_id": "RATE.UST.10Y", "weight": 1}]})
    assert bad.status_code == 422
    secured.cookies.clear()
    _signup(secured, "ivy@example.test", "Ivy")
    assert secured.get(f"/api/v1/portfolios/{pid}/risk").status_code == 404
    assert pid not in {p["portfolio_id"] for p in secured.get("/api/v1/portfolios").json()}


def test_password_reset_flow_and_no_enumeration(secured):
    _signup(secured, "jules@example.test", "Jules")
    unknown = secured.post("/api/v1/auth/password-reset/request", json={"email": "nobody@example.test"})
    known = secured.post("/api/v1/auth/password-reset/request", json={"email": "jules@example.test"})
    assert unknown.status_code == known.status_code == 202 and unknown.json() == known.json()

    token = auth.request_password_reset("jules@example.test", client="test")
    assert secured.post("/api/v1/auth/password-reset/confirm", json={"token": token, "password": "short"}).status_code == 422
    assert secured.post("/api/v1/auth/password-reset/confirm", json={"token": token, "password": "a brand new passphrase"}).status_code == 204
    # single use; old sessions are revoked
    assert secured.post("/api/v1/auth/password-reset/confirm", json={"token": token, "password": "another new passphrase"}).status_code == 400
    assert secured.get("/api/v1/account").status_code == 401
    assert secured.post("/api/v1/auth/login", json={"email": "jules@example.test", "password": "correct horse battery"}).status_code == 401
    assert secured.post("/api/v1/auth/login", json={"email": "jules@example.test", "password": "a brand new passphrase"}).status_code == 200


def test_change_password_requires_current(secured):
    _signup(secured, "kim@example.test", "Kim")
    bad = secured.post("/api/v1/account/password", json={"current_password": "wrong wrong wrong", "new_password": "next passphrase ok"})
    assert bad.status_code == 400
    ok = secured.post("/api/v1/account/password", json={"current_password": "correct horse battery", "new_password": "next passphrase ok"})
    assert ok.status_code == 204
    assert secured.get("/api/v1/account").status_code == 401  # signed out everywhere
    assert secured.post("/api/v1/auth/login", json={"email": "kim@example.test", "password": "next passphrase ok"}).status_code == 200


def test_monitoring_agents_fire_run_research_and_are_scoped(secured):
    _signup(secured, "liz@example.test", "Liz")
    body = {"name": "WTI big move", "watch": ["CMD.CRUDE.WTI"],
            "triggers": [{"type": "pct_move", "abs_pct": 0.0001, "window_days": 30}],  # always fires on synthetic data
            "action": {"template": "commodity_deep_dive", "query": "What triggered this?", "engine": "deterministic"}}
    agent = secured.post("/api/v1/agents", json=body).json()
    from atlas import agents
    from atlas.research import engine

    r = agents.check_agent(agent["agent_id"], background=False)
    assert r["fired"] and r["run_id"]
    run = engine.get_run(r["run_id"])
    assert run["status"] == "complete" and run["result"]["verification"]["passed"]
    assert agents.check_agent(agent["agent_id"], background=False)["suppressed"]  # cooldown
    listed = secured.get("/api/v1/agents").json()
    events = listed["agents"][0]["events"]  # newest first: the suppressed check, then the firing
    assert listed["unread"] == 1 and [e["status"] for e in events] == ["suppressed", "fired"]
    assert events[1]["run_id"] == r["run_id"]
    assert secured.post("/api/v1/agents/inbox/read").status_code == 204  # not captured by /{agent_id}/{action}
    assert secured.get("/api/v1/agents").json()["unread"] == 0
    # quota: explorer allows one agent
    assert secured.post("/api/v1/agents", json=body).status_code == 429
    bad = secured.post("/api/v1/agents", json={**body, "triggers": [{"type": "nope"}]})
    assert bad.status_code in (422, 429)
    secured.cookies.clear()
    _signup(secured, "max@example.test", "Max")
    assert secured.post(f"/api/v1/agents/{agent['agent_id']}/check").status_code == 404
    assert secured.get("/api/v1/agents").json()["agents"] == []
