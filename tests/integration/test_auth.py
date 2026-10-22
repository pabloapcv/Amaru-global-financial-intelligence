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
    # A follow-up cannot use another workspace's run as context.
    assert secured.post("/api/v1/research", json={"query": "and then?", "parent_run_id": run_a}).status_code == 404
    assert engine.prior_context(run_a, "some-other-workspace") is None


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


def test_email_verification_flow_and_optional_enforcement(secured, monkeypatch):
    acct = _signup(secured, "nora@example.test", "Nora")
    assert acct["email_verified"] is False
    monkeypatch.setenv("ATLAS_REQUIRE_EMAIL_VERIFICATION", "true")
    blocked = secured.post("/api/v1/research", json={"query": "copper outlook", "engine": "deterministic"})
    assert blocked.status_code == 403 and "verify your email" in blocked.json()["detail"]
    assert secured.get("/api/v1/instruments").status_code == 200  # reading data stays open
    token = auth.send_verification(acct["user"]["user_id"])
    assert secured.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 204
    assert secured.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 400  # single use
    assert secured.get("/api/v1/account").json()["email_verified"] is True
    ok = secured.post("/api/v1/research", json={"query": "copper outlook", "engine": "deterministic", "depth": "quick"})
    assert ok.status_code == 202
    assert secured.post("/api/v1/account/verify-email/resend").json()["status"] == "already_verified"


def test_operator_created_users_are_verified():
    uid, _ = auth.create_user("ops@example.test", "operator passphrase", verified=True)
    assert auth.send_verification(uid) is None


def test_operator_set_plan_changes_limits():
    uid, ws = auth.create_user("plan@example.test", "operator passphrase", verified=True)
    assert auth.account(auth.Principal(uid, ws, "owner", "plan@example.test", "session"))["workspace"]["plan"] == "explorer"
    auth.set_plan(ws, "research_pro")
    acct = auth.account(auth.Principal(uid, ws, "owner", "plan@example.test", "session"))
    assert acct["workspace"]["plan"] == "research_pro" and acct["limits"]["agents"] == auth.PLANS["research_pro"]["agents"]
    with pytest.raises(auth.ValidationError):
        auth.set_plan(ws, "platinum")
    assert any(w["workspace_id"] == ws and w["plan"] == "research_pro" for w in auth.list_workspaces())


def test_onboarding_checklist_tracks_real_progress(secured):
    _signup(secured, "olga@example.test", "Olga")
    steps = {s["key"]: s["done"] for s in secured.get("/api/v1/onboarding").json()["steps"]}
    assert steps == {"research": False, "portfolio": False, "agent": False, "api_key": False}
    ex = secured.post("/api/v1/onboarding/example-portfolio").json()
    assert ex["name"].startswith("Example") and secured.post("/api/v1/onboarding/example-portfolio").json()["portfolio_id"] == ex["portfolio_id"]
    secured.post("/api/v1/api-keys", json={"name": "nb"})
    steps = {s["key"]: s["done"] for s in secured.get("/api/v1/onboarding").json()["steps"]}
    assert steps["portfolio"] and steps["api_key"] and not steps["research"]


def test_report_share_links_are_scoped_hashed_expiring_and_revocable(secured):
    from datetime import timedelta

    from sqlalchemy import select, update

    from atlas.data.db import AuditEvent, ReportShare, session_scope, utcnow
    from atlas.research import engine

    a = _signup(secured, "sharer@example.test", "Sharer")
    run = engine.create_run("copper outlook", {"engine": "deterministic", "depth": "quick", "formats": ["html", "xlsx"]},
                            a["user"]["user_id"], a["workspace"]["workspace_id"])
    report_id = engine.execute_run(run)["report_id"]

    r = secured.post(f"/api/v1/reports/{report_id}/shares", json={"days": 7})
    assert r.status_code == 201, r.text
    link = r.json()
    token, share_id = link["token"], link["share_id"]
    assert link["path"] == f"/share/{token}" and token.startswith("shr_") and len(token) > 40
    with session_scope() as s:
        row = s.get(ReportShare, share_id)
        assert row.token_hash != token and token not in row.token_hash  # only the hash is stored
    listed = secured.get(f"/api/v1/reports/{report_id}/shares").json()
    assert listed[0]["share_id"] == share_id and "token" not in listed[0]

    # Anyone with the link sees the report, sandboxed, without signing in.
    secured.cookies.clear()
    v = secured.get(f"/share/{token}")
    assert v.status_code == 200 and "text/html" in v.headers["content-type"]
    assert v.headers["content-security-policy"].startswith("sandbox;") and "script-src" not in v.headers["content-security-policy"]
    assert v.headers["x-robots-tag"].startswith("noindex") and v.headers["cache-control"] == "private, no-store"
    assert secured.get(f"/share/{token}", params={"format": "xlsx"}).status_code == 200
    assert secured.get("/share/shr_not-a-real-token").status_code == 404
    # The link opens the report only: the API still needs credentials.
    assert secured.get(f"/api/v1/reports/{report_id}").status_code == 401
    with session_scope() as s:
        assert s.get(ReportShare, share_id).view_count == 2
        events = [e.action for e in s.execute(select(AuditEvent).where(AuditEvent.workspace_id == a["workspace"]["workspace_id"])).scalars()]
        assert "report.shared" in events and "report.share_viewed" in events
        assert all(token not in str(e.detail) for e in s.execute(select(AuditEvent)).scalars())

    # Another workspace cannot share, list or revoke it.
    _signup(secured, "outsider@example.test", "Outsider")
    assert secured.post(f"/api/v1/reports/{report_id}/shares", json={"days": 7}).status_code == 404
    assert secured.get(f"/api/v1/reports/{report_id}/shares").json() == []
    assert secured.delete(f"/api/v1/shares/{share_id}").status_code == 404
    assert secured.post(f"/api/v1/reports/{report_id}/shares", json={"days": 365}).status_code == 422

    # Expiry and revocation close the link.
    with session_scope() as s:
        s.execute(update(ReportShare).where(ReportShare.share_id == share_id).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert secured.get(f"/share/{token}").status_code == 404
    with session_scope() as s:
        s.execute(update(ReportShare).where(ReportShare.share_id == share_id).values(expires_at=utcnow() + timedelta(days=1)))
    secured.cookies.clear()
    secured.post("/api/v1/auth/login", json={"email": "sharer@example.test", "password": "correct horse battery"})
    assert secured.delete(f"/api/v1/shares/{share_id}").status_code == 204
    assert secured.get(f"/share/{token}").status_code == 404
    assert secured.get(f"/api/v1/reports/{report_id}/shares").json()[0]["status"] == "revoked"


def test_team_invitations_roles_seats_and_removal(secured):
    from datetime import timedelta

    from sqlalchemy import update

    from atlas.data.db import Invitation, session_scope, utcnow

    owner = _signup(secured, "owner@team.test", "Team A")
    ws = owner["workspace"]["workspace_id"]
    # Explorer is a one-seat plan: inviting needs a bigger plan.
    assert secured.post("/api/v1/team/invitations", json={"email": "new@team.test"}).status_code == 429
    auth.set_plan(ws, "investor")  # 2 seats
    r = secured.post("/api/v1/team/invitations", json={"email": "New@Team.test", "role": "member"})
    assert r.status_code == 201, r.text
    inv = r.json()
    token = inv["link"].split("token=")[1]
    assert inv["email"] == "new@team.test" and token.startswith("inv_")
    assert secured.get("/api/v1/team").json()["seats"] == {"used": 2, "limit": 2}  # pending invite holds a seat
    assert secured.post("/api/v1/team/invitations", json={"email": "third@team.test"}).status_code == 429

    # The invitee creates their account through the link and lands in the workspace.
    secured.cookies.clear()
    details = secured.get(f"/api/v1/auth/invitations/{token}").json()
    assert details["workspace_name"] == "Team A" and details["email"] == "new@team.test" and not details["has_account"]
    joined = secured.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "another good pass", "name": "New"})
    assert joined.status_code == 200, joined.text
    assert joined.json()["workspace"]["workspace_id"] == ws and joined.json()["role"] == "member"
    member_session = secured.cookies.get("atlas_session")
    member_key = secured.post("/api/v1/api-keys", json={"name": "member key"}).json()["key"]
    # Single use.
    secured.cookies.clear()
    assert secured.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "another good pass"}).status_code == 400
    assert secured.get(f"/api/v1/auth/invitations/{token}").status_code == 404

    # Members can see the team but not manage it.
    secured.cookies.set("atlas_session", member_session)
    assert {m["email"] for m in secured.get("/api/v1/team").json()["members"]} == {"owner@team.test", "new@team.test"}
    assert secured.post("/api/v1/team/invitations", json={"email": "x@team.test"}).status_code == 403
    assert secured.delete(f"/api/v1/team/members/{owner['user']['user_id']}").status_code == 403

    # An existing user elsewhere accepts while signed in, and can switch between workspaces.
    secured.cookies.clear()
    other = _signup(secured, "analyst@team.test", "Analyst home")
    analyst_session = secured.cookies.get("atlas_session")
    secured.cookies.clear()
    secured.post("/api/v1/auth/login", json={"email": "owner@team.test", "password": "correct horse battery"})
    auth.set_plan(ws, "research_pro")
    analyst_token = secured.post("/api/v1/team/invitations", json={"email": "analyst@team.test", "role": "admin"}).json()["link"].split("token=")[1]
    wrong_token = secured.post("/api/v1/team/invitations", json={"email": "someone.else@team.test"}).json()["link"].split("token=")[1]
    secured.cookies.clear()
    secured.cookies.set("atlas_session", analyst_session)
    assert secured.post("/api/v1/auth/invitations/accept", json={"token": wrong_token}).status_code == 400  # not their invitation
    acc = secured.post("/api/v1/auth/invitations/accept", json={"token": analyst_token}).json()
    assert acc["workspace"]["workspace_id"] == ws and acc["role"] == "admin"
    assert {w["workspace_id"] for w in acc["workspaces"]} == {ws, other["workspace"]["workspace_id"]}
    back = secured.post("/api/v1/auth/switch-workspace", json={"workspace_id": other["workspace"]["workspace_id"]})
    assert back.status_code == 200 and back.json()["role"] == "owner"
    secured.cookies.clear()
    secured.post("/api/v1/auth/login", json={"email": "new@team.test", "password": "another good pass"})
    assert secured.post("/api/v1/auth/switch-workspace", json={"workspace_id": other["workspace"]["workspace_id"]}).status_code == 403

    # Only the owner changes roles; nobody removes or demotes the owner.
    secured.cookies.clear()
    secured.post("/api/v1/auth/login", json={"email": "owner@team.test", "password": "correct horse battery"})
    assert secured.patch(f"/api/v1/team/members/{owner['user']['user_id']}", json={"role": "member"}).status_code == 403
    assert secured.delete(f"/api/v1/team/members/{owner['user']['user_id']}").status_code == 403
    member_id = next(m["user_id"] for m in secured.get("/api/v1/team").json()["members"] if m["email"] == "new@team.test")
    # Removing a member ends their sessions and API keys in this workspace at once.
    assert secured.delete(f"/api/v1/team/members/{member_id}").status_code == 204
    secured.cookies.clear()
    secured.cookies.set("atlas_session", member_session)
    assert secured.get("/api/v1/account").status_code == 401
    secured.cookies.clear()
    assert secured.get("/api/v1/account", headers={"Authorization": f"Bearer {member_key}"}).status_code == 401

    # Expired invitations stop working.
    secured.post("/api/v1/auth/login", json={"email": "owner@team.test", "password": "correct horse battery"})
    late = secured.post("/api/v1/team/invitations", json={"email": "late@team.test"}).json()
    with session_scope() as s:
        s.execute(update(Invitation).where(Invitation.invitation_id == late["invitation_id"]).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert secured.get(f"/api/v1/auth/invitations/{late['link'].split('token=')[1]}").status_code == 404


def test_activity_log_is_scoped_paginated_and_manager_only(secured):
    owner = _signup(secured, "auditor@act.test", "Audited")
    ws = owner["workspace"]["workspace_id"]
    key_id = secured.post("/api/v1/api-keys", json={"name": "k1"}).json()["key_id"]
    assert secured.delete(f"/api/v1/api-keys/{key_id}").status_code in (200, 204)
    page = secured.get("/api/v1/activity").json()
    actions = [e["action"] for e in page["events"]]
    assert actions[:2] == ["api_key.revoked", "api_key.created"] and "login.succeeded" in actions
    assert all(e["actor"] == "auditor@act.test" for e in page["events"] if e["action"].startswith("api_key."))
    keys = secured.get("/api/v1/activity", params={"category": "keys"}).json()["events"]
    assert [e["action"] for e in keys] == ["api_key.revoked", "api_key.created"]
    # Pagination walks every event exactly once, even within the same second.
    seen, cursor = [], None
    while True:
        page = secured.get("/api/v1/activity", params={"limit": 1, **({"cursor": cursor} if cursor else {})}).json()
        seen += [e["event_id"] for e in page["events"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [e["event_id"] for e in secured.get("/api/v1/activity", params={"limit": 500}).json()["events"]]
    assert len(seen) == len(set(seen)) >= 4
    assert secured.get("/api/v1/activity", params={"cursor": "not-a-date|x"}).status_code == 422
    csv_body = secured.get("/api/v1/activity", params={"format": "csv"}).text
    assert csv_body.splitlines()[0] == "at,action,label,actor,detail" and "api_key.created" in csv_body

    # Another workspace sees none of it; members cannot see activity.
    secured.cookies.clear()
    _signup(secured, "stranger@act.test", "Elsewhere")
    assert all(e["actor"] != "auditor@act.test" for e in secured.get("/api/v1/activity").json()["events"])
    secured.cookies.clear()
    secured.post("/api/v1/auth/login", json={"email": "auditor@act.test", "password": "correct horse battery"})
    auth.set_plan(ws, "investor")
    token = secured.post("/api/v1/team/invitations", json={"email": "plain.member@act.test"}).json()["link"].split("token=")[1]
    secured.cookies.clear()
    secured.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "member password!"})
    assert secured.get("/api/v1/activity").status_code == 403


def test_watchlist_is_per_workspace_validated_and_capped(secured, monkeypatch):
    from atlas import watchlist

    _signup(secured, "watcher@wl.test", "Watchers")
    assert secured.post("/api/v1/watchlist/CMD.SUGAR.ISA").json()["dataset_ids"] == ["CMD.SUGAR.ISA"]
    assert secured.post("/api/v1/watchlist/CMD.SUGAR.ISA").json()["dataset_ids"] == ["CMD.SUGAR.ISA"]  # idempotent
    secured.post("/api/v1/watchlist/US.CPI.HEADLINE")
    r = secured.put("/api/v1/watchlist", json={"dataset_ids": ["US.CPI.HEADLINE", "CMD.SUGAR.ISA"]})
    assert r.json()["dataset_ids"] == ["US.CPI.HEADLINE", "CMD.SUGAR.ISA"]
    assert secured.post("/api/v1/watchlist/FUND.SUGAR").status_code == 422  # not a quotable series
    assert secured.post("/api/v1/watchlist/NOPE").status_code == 422
    assert secured.put("/api/v1/watchlist", json={"dataset_ids": ["CMD.SUGAR.ISA", "CMD.GOLD"]}).status_code == 422  # no source
    monkeypatch.setattr(watchlist, "MAX_ITEMS", 2)
    assert secured.post("/api/v1/watchlist/FX.USDBRL").status_code == 422
    # Another workspace has its own (empty) list.
    secured.cookies.clear()
    _signup(secured, "other@wl.test", "Others")
    assert secured.get("/api/v1/watchlist").json()["dataset_ids"] == []
    secured.cookies.clear()
    secured.post("/api/v1/auth/login", json={"email": "watcher@wl.test", "password": "correct horse battery"})
    assert secured.delete("/api/v1/watchlist/CMD.SUGAR.ISA").json()["dataset_ids"] == ["US.CPI.HEADLINE"]


def test_alerts_fire_once_per_episode_and_email_their_creator(secured, monkeypatch):
    from atlas import intelligence, mailer
    from atlas.data.access import get_series

    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: (subject.startswith("ATLAS alert") and sent.append((to, subject))) or True)
    owner = _signup(secured, "alerter@al.test", "Alerting")
    ws = owner["workspace"]["workspace_id"]
    last = float(get_series("CMD.SUGAR.ISA")[0].iloc[-1])
    r = secured.post("/api/v1/alerts", json={"name": "Sugar above", "notify_email": True,
                                             "rule": {"type": "threshold", "dataset_id": "CMD.SUGAR.ISA", "op": ">", "value": last - 1}})
    assert r.status_code == 201 and r.json()["notify_email"] is True
    alert_id = r.json()["alert_id"]

    first = intelligence.evaluate_alerts({"CMD.SUGAR.ISA"}, workspace_id=ws)
    assert [f["alert_id"] for f in first] == [alert_id] and sent == [("alerter@al.test", "ATLAS alert: Sugar above")]
    assert intelligence.evaluate_alerts({"CMD.SUGAR.ISA"}, workspace_id=ws) == []  # still holding: no repeat
    assert len(sent) == 1

    # Condition stops holding (threshold moved above the price), which re-arms; when it holds again it fires again.
    from atlas.data.db import Alert, session_scope

    with session_scope() as s:
        s.get(Alert, alert_id).rule = {"type": "threshold", "dataset_id": "CMD.SUGAR.ISA", "op": ">", "value": last + 1}
    assert intelligence.evaluate_alerts({"CMD.SUGAR.ISA"}, workspace_id=ws) == []
    with session_scope() as s:
        s.get(Alert, alert_id).rule = {"type": "threshold", "dataset_id": "CMD.SUGAR.ISA", "op": ">", "value": last - 1}
    assert len(intelligence.evaluate_alerts({"CMD.SUGAR.ISA"}, workspace_id=ws)) == 1 and len(sent) == 2
    listed = next(a for a in secured.get("/api/v1/alerts").json() if a["alert_id"] == alert_id)
    assert listed["notify_email"] and listed["mine"] and len(listed["events"]) == 2

    # Email can be switched off by anyone in the workspace, but only the creator can switch it on.
    assert secured.patch(f"/api/v1/alerts/{alert_id}", json={"notify_email": False}).json()["notify_email"] is False
    auth.set_plan(ws, "investor")
    token = secured.post("/api/v1/team/invitations", json={"email": "colleague@al.test"}).json()["link"].split("token=")[1]
    secured.cookies.clear()
    secured.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "colleague password"})
    assert secured.patch(f"/api/v1/alerts/{alert_id}", json={"notify_email": True}).status_code == 403
    secured.cookies.clear()
    _signup(secured, "outsider@al.test", "Outside")
    assert secured.patch(f"/api/v1/alerts/{alert_id}", json={"notify_email": False}).status_code == 404


def test_deleting_research_cascades_but_keeps_accounting(secured):
    from pathlib import Path

    from sqlalchemy import select

    from atlas.config import settings
    from atlas.data.db import Report, ReportShare, ResearchEvidence, ResearchRun, session_scope
    from atlas.research import engine

    owner = _signup(secured, "historian@hist.test", "History")
    ws, uid = owner["workspace"]["workspace_id"], owner["user"]["user_id"]
    run = engine.create_run("copper outlook", {"engine": "deterministic", "depth": "quick", "formats": ["html", "xlsx"]}, uid, ws)
    report_id = engine.execute_run(run)["report_id"]
    child = engine.create_run("what about over ten years?", {"engine": "deterministic", "depth": "quick", "formats": [],
                                                             "parent_run_id": run}, uid, ws)
    engine.execute_run(child)
    token = secured.post(f"/api/v1/reports/{report_id}/shares", json={"days": 7}).json()["token"]
    with session_scope() as s:
        s.get(ResearchRun, run).cost_usd = 0.42  # pretend Claude was used
    report_dir = Path(settings.object_store) / "reports" / report_id
    assert report_dir.exists()

    assert [r["run_id"] for r in secured.get("/api/v1/research", params={"q": "COPPER"}).json()] == [run]
    assert secured.delete(f"/api/v1/research/{run}").status_code == 204
    assert secured.get(f"/api/v1/research/{run}").status_code == 404
    assert run not in {r["run_id"] for r in secured.get("/api/v1/research").json()}
    assert secured.get(f"/api/v1/reports/{report_id}").status_code == 404
    assert secured.get(f"/share/{token}").status_code == 404
    assert not report_dir.exists()
    assert secured.get(f"/api/v1/research/{child}").json()["parent"] is None
    with session_scope() as s:
        row = s.get(ResearchRun, run)
        assert row.status == "deleted" and row.query == "[deleted]" and row.result is None and row.cost_usd == 0.42
        assert not s.execute(select(ResearchEvidence).where(ResearchEvidence.run_id == run)).first()
        assert not s.execute(select(Report).where(Report.run_id == run)).first()
        assert not s.execute(select(ReportShare).where(ReportShare.report_id == report_id)).first()
    assert auth.llm_spend_this_month(ws) >= 0.42  # deleting cannot reset the Claude budget
    assert secured.delete(f"/api/v1/research/{run}").status_code == 404

    # A plain member cannot delete someone else's run; other workspaces cannot see it at all.
    auth.set_plan(ws, "investor")
    tok = secured.post("/api/v1/team/invitations", json={"email": "member@hist.test"}).json()["link"].split("token=")[1]
    secured.cookies.clear()
    secured.post("/api/v1/auth/invitations/accept", json={"token": tok, "password": "member password!"})
    assert secured.delete(f"/api/v1/research/{child}").status_code == 403
    secured.cookies.clear()
    _signup(secured, "nosy@hist.test", "Nosy")
    assert secured.delete(f"/api/v1/research/{child}").status_code == 404


def test_two_factor_authentication(secured, monkeypatch):
    from atlas import secretbox, totp
    from atlas.data.db import LoginChallenge, User, session_scope

    secretbox._fernet.cache_clear()
    acct = _signup(secured, "careful@twofa.test", "Careful")
    other_device = TestClient(secured.app)
    other_device.post("/api/v1/auth/login", json={"email": "careful@twofa.test", "password": "correct horse battery"})
    api_key = secured.post("/api/v1/api-keys", json={"name": "script"}).json()["key"]

    setup = secured.post("/api/v1/account/2fa/setup").json()
    secret = setup["secret"]
    assert setup["otpauth_uri"].startswith("otpauth://totp/ATLAS:careful%40twofa.test?secret=")
    with session_scope() as s:
        stored = s.get(User, acct["user"]["user_id"]).totp_secret_enc
    assert secret not in stored and secretbox.decrypt(stored) == secret  # encrypted at rest
    assert secured.post("/api/v1/account/2fa/enable", json={"code": "000000"}).status_code == 422
    step = totp.current_step()
    on = secured.post("/api/v1/account/2fa/enable", json={"code": totp.code_at(secret, step)}).json()
    codes = on["recovery_codes"]
    assert on["enabled"] and len(codes) == 10 and len(set(codes)) == 10
    assert secured.get("/api/v1/account").json()["two_factor"] is True  # this session stays
    assert other_device.get("/api/v1/account").status_code == 401  # password-only sessions end
    assert secured.get("/api/v1/account", headers={"Authorization": f"Bearer {api_key}"}).status_code == 200  # keys unaffected

    # Password alone no longer signs in.
    secured.cookies.clear()
    r = secured.post("/api/v1/auth/login", json={"email": "careful@twofa.test", "password": "correct horse battery"}).json()
    assert r == {"two_factor_required": True, "challenge": r["challenge"]} and "atlas_session" not in secured.cookies
    assert secured.post("/api/v1/auth/login/2fa", json={"challenge": r["challenge"], "code": "123456"}).status_code == 401
    # The code used to enable cannot be replayed; the next step's code works.
    assert secured.post("/api/v1/auth/login/2fa", json={"challenge": r["challenge"], "code": totp.code_at(secret, step)}).status_code == 401
    ok = secured.post("/api/v1/auth/login/2fa", json={"challenge": r["challenge"], "code": totp.code_at(secret, step + 1)})
    assert ok.status_code == 200 and secured.get("/api/v1/account").status_code == 200
    assert secured.post("/api/v1/auth/login/2fa", json={"challenge": r["challenge"], "code": totp.code_at(secret, step + 1)}).status_code == 401  # single use

    # A recovery code works once.
    secured.cookies.clear()
    ch = secured.post("/api/v1/auth/login", json={"email": "careful@twofa.test", "password": "correct horse battery"}).json()["challenge"]
    assert secured.post("/api/v1/auth/login/2fa", json={"challenge": ch, "code": codes[0].upper()}).status_code == 200
    assert secured.get("/api/v1/account").json()["recovery_codes_left"] == 9
    secured.cookies.clear()
    ch = secured.post("/api/v1/auth/login", json={"email": "careful@twofa.test", "password": "correct horse battery"}).json()["challenge"]
    assert secured.post("/api/v1/auth/login/2fa", json={"challenge": ch, "code": codes[0]}).status_code == 401
    # Challenges allow a few attempts and expire.
    for _ in range(4):
        secured.post("/api/v1/auth/login/2fa", json={"challenge": ch, "code": "000000"})
    assert secured.post("/api/v1/auth/login/2fa", json={"challenge": ch, "code": codes[1]}).status_code == 401  # 6th attempt
    ch2 = secured.post("/api/v1/auth/login", json={"email": "careful@twofa.test", "password": "correct horse battery"}).json()["challenge"]
    from datetime import timedelta

    from sqlalchemy import update

    from atlas.data.db import utcnow

    with session_scope() as s:
        s.execute(update(LoginChallenge).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert secured.post("/api/v1/auth/login/2fa", json={"challenge": ch2, "code": codes[1]}).status_code == 401

    # Turning it off needs the password and a code.
    ch3 = secured.post("/api/v1/auth/login", json={"email": "careful@twofa.test", "password": "correct horse battery"}).json()["challenge"]
    secured.post("/api/v1/auth/login/2fa", json={"challenge": ch3, "code": codes[1]})
    assert secured.post("/api/v1/account/2fa/disable", json={"password": "wrong password!!", "code": codes[2]}).status_code == 403
    assert secured.post("/api/v1/account/2fa/disable", json={"password": "correct horse battery", "code": codes[3]}).status_code == 204
    secured.cookies.clear()
    assert "token" in secured.post("/api/v1/auth/login", json={"email": "careful@twofa.test", "password": "correct horse battery"}).json()

    # Operators can turn it off for a user who lost everything.
    secured.post("/api/v1/account/2fa/setup")
    with session_scope() as s:
        sec = secretbox.decrypt(s.execute(select_user("careful@twofa.test")).scalar_one().totp_secret_enc)
    secured.post("/api/v1/account/2fa/enable", json={"code": totp.code_at(sec, totp.current_step())})
    assert auth.operator_disable_2fa("careful@twofa.test") and not auth.operator_disable_2fa("careful@twofa.test")


def select_user(email):
    from sqlalchemy import select

    from atlas.data.db import User

    return select(User).where(User.email == email)
