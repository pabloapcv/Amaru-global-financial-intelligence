"""Outbound webhooks: signing, events, retries, SSRF guard, scoping."""

import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas import auth


@pytest.fixture()
def secured(seeded, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTH", "required")
    from atlas.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def receiver(monkeypatch):
    """A fake endpoint: records requests and answers with the next queued status (default 200)."""
    from atlas import webhooks

    got, statuses = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        return httpx.Response(statuses.pop(0) if statuses else 200)

    monkeypatch.setattr(webhooks, "client_factory", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(webhooks, "resolve", lambda host: ["10.0.0.5"] if host.startswith("internal") else ["93.184.216.34"])
    monkeypatch.setattr(webhooks, "BACKGROUND", False)
    return got, statuses


def _signup(client, email, workspace):
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": "correct horse battery", "name": email.split("@")[0], "workspace_name": workspace})
    assert r.status_code == 201, r.text
    return r.json()


def _verify(secret: str, request: httpx.Request) -> dict:
    parts = dict(kv.split("=", 1) for kv in request.headers["X-Atlas-Signature"].split(","))
    expected = hmac.new(secret.encode(), f"{parts['t']}.".encode() + request.content, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected, parts["v1"])
    return json.loads(request.content)


def test_webhooks_are_validated_signed_scoped_and_audited(secured, receiver):
    from sqlalchemy import select

    from atlas.data.db import AuditEvent, Webhook, session_scope

    got, _ = receiver
    acct = _signup(secured, "hooks@int.test", "Integrations")
    ws = acct["workspace"]["workspace_id"]
    bad = [("http://hooks.example.test/x", "https"), ("https://internal.example.test/x", "private"),
           ("https://user:pw@hooks.example.test/x", "credentials")]
    for url, why in bad:
        r = secured.post("/api/v1/webhooks", json={"url": url, "events": ["alert.triggered"]})
        assert r.status_code == 422 and why in r.text
    assert secured.post("/api/v1/webhooks", json={"url": "https://hooks.example.test/x", "events": ["nope"]}).status_code == 422

    r = secured.post("/api/v1/webhooks", json={"url": "https://hooks.example.test/atlas", "events": ["alert.triggered", "research.completed"],
                                               "description": "ops"})
    assert r.status_code == 201
    hook = r.json()
    secret = hook["secret"]
    assert secret.startswith("whsec_")
    assert secret not in str(secured.get("/api/v1/webhooks").json())  # shown once
    with session_scope() as s:
        assert secret not in s.get(Webhook, hook["webhook_id"]).secret_enc  # encrypted at rest
    # Explorer allows one webhook.
    assert secured.post("/api/v1/webhooks", json={"url": "https://hooks.example.test/2", "events": ["agent.fired"]}).status_code == 429

    t = secured.post(f"/api/v1/webhooks/{hook['webhook_id']}/test").json()
    assert t["status"] == "delivered" and t["response_code"] == 200
    body = _verify(secret, got[-1])
    assert body["event"] == "ping" and body["workspace_id"] == ws and got[-1].headers["X-Atlas-Event"] == "ping"

    rotated = secured.post(f"/api/v1/webhooks/{hook['webhook_id']}/rotate-secret").json()["secret"]
    secured.post(f"/api/v1/webhooks/{hook['webhook_id']}/test")
    _verify(rotated, got[-1])

    # Other workspaces cannot see or touch it; plain members cannot manage webhooks.
    other = TestClient(secured.app)
    _signup(other, "else@int.test", "Elsewhere")
    assert other.get("/api/v1/webhooks").json()["webhooks"] == []
    for call in (other.post(f"/api/v1/webhooks/{hook['webhook_id']}/test"), other.delete(f"/api/v1/webhooks/{hook['webhook_id']}"),
                 other.get(f"/api/v1/webhooks/{hook['webhook_id']}/deliveries")):
        assert call.status_code == 404
    auth.set_plan(ws, "investor")
    token = secured.post("/api/v1/team/invitations", json={"email": "member@int.test"}).json()["link"].split("token=")[1]
    member = TestClient(secured.app)
    member.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "member password!"})
    assert member.get("/api/v1/webhooks").status_code == 403

    key = secured.post("/api/v1/api-keys", json={"name": "ci"}).json()["key"]
    assert secured.post("/api/v1/webhooks", json={"url": "https://hooks.example.test/k", "events": ["agent.fired"]},
                        headers={"Authorization": f"Bearer {key}"}).status_code == 403

    assert secured.delete(f"/api/v1/webhooks/{hook['webhook_id']}").status_code == 204
    with session_scope() as s:
        actions = {e.action for e in s.execute(select(AuditEvent).where(AuditEvent.workspace_id == ws)).scalars()}
    assert {"webhook.created", "webhook.secret_rotated", "webhook.deleted"} <= actions
    assert any(e["action"] == "webhook.created" for e in secured.get("/api/v1/activity?category=integrations").json()["events"])


def test_events_reach_subscribed_webhooks_with_slack_format(secured, receiver):
    from atlas import intelligence
    from atlas.data.access import get_series
    from atlas.research import engine

    got, _ = receiver
    acct = _signup(secured, "events@int.test", "Events")
    ws = acct["workspace"]["workspace_id"]
    auth.set_plan(ws, "investor")
    atlas_hook = secured.post("/api/v1/webhooks", json={"url": "https://hooks.example.test/a", "events": ["alert.triggered", "research.completed"]}).json()
    secured.post("/api/v1/webhooks", json={"url": "https://hooks.slack.test/services/T/B/x", "events": ["alert.triggered"], "format": "slack"})

    last = float(get_series("CMD.SUGAR.ISA")[0].iloc[-1])
    secured.post("/api/v1/alerts", json={"name": "Sugar above", "rule": {"type": "threshold", "dataset_id": "CMD.SUGAR.ISA", "op": ">", "value": last - 1}})
    intelligence.evaluate_alerts({"CMD.SUGAR.ISA"}, workspace_id=ws)
    by_url = {str(r.url): r for r in got}
    alert = _verify(atlas_hook["secret"], by_url["https://hooks.example.test/a"])
    assert alert["event"] == "alert.triggered" and alert["data"]["name"] == "Sugar above" and "Sugar" in alert["data"]["message"]
    slack = json.loads(by_url["https://hooks.slack.test/services/T/B/x"].content)
    assert set(slack) == {"text"} and "ATLAS alert: Sugar above" in slack["text"]

    got.clear()
    run_id = engine.create_run("Sugar price outlook", {"engine": "deterministic", "formats": []}, acct["user"]["user_id"], ws)
    engine.execute_run(run_id)
    assert len(got) == 1  # only the subscribed webhook
    done = _verify(atlas_hook["secret"], got[0])
    assert done["event"] == "research.completed" and done["data"]["run_id"] == run_id and done["data"]["trigger"] == "user"
    assert done["data"]["research_url"].endswith(f"/research?run={run_id}")

    # Paused webhooks receive nothing.
    secured.patch(f"/api/v1/webhooks/{atlas_hook['webhook_id']}", json={"status": "paused"})
    got.clear()
    engine.execute_run(engine.create_run("Sugar price outlook again", {"engine": "deterministic", "formats": []}, acct["user"]["user_id"], ws))
    assert got == []


def test_failed_deliveries_retry_with_backoff_then_disable(secured, receiver, monkeypatch):
    from atlas import mailer, webhooks
    from atlas.data.db import Webhook, WebhookDelivery, session_scope, utcnow

    got, statuses = receiver
    mails = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: (subject == "ATLAS webhook disabled" and mails.append(to)) or True)
    acct = _signup(secured, "flaky@int.test", "Flaky")
    ws = acct["workspace"]["workspace_id"]
    hook = secured.post("/api/v1/webhooks", json={"url": "https://hooks.example.test/flaky", "events": ["research.failed"]}).json()

    statuses.extend([500, 503])
    [did] = webhooks.emit(ws, "research.failed", {"run_id": "r1"})
    with session_scope() as s:
        d = s.get(WebhookDelivery, did)
        assert d.status == "pending" and d.attempts == 1 and d.error == "HTTP 500"
        first_retry = d.next_attempt_at
    assert webhooks.deliver_due() == 0  # not due yet
    assert webhooks.deliver_due(first_retry + timedelta(seconds=1)) == 1
    with session_scope() as s:
        assert s.get(WebhookDelivery, did).attempts == 2
    later = utcnow() + timedelta(hours=1)
    webhooks.deliver_due(later)
    with session_scope() as s:
        d = s.get(WebhookDelivery, did)
        assert d.status == "delivered" and d.attempts == 3
        assert s.get(Webhook, hook["webhook_id"]).consecutive_failures == 0
    ids = {json.loads(r.content)["id"] for r in got}
    assert ids == {did}  # retries keep the same id
    listed = secured.get(f"/api/v1/webhooks/{hook['webhook_id']}/deliveries").json()
    assert listed[0]["status"] == "delivered" and listed[0]["attempts"] == 3

    # A delivery gives up after the last retry; an endpoint failing again and again is disabled.
    statuses.extend([500] * 40)
    t = utcnow()
    for _ in range(3):
        webhooks.emit(ws, "research.failed", {"run_id": "r2"})
    for step in range(1, 8):
        webhooks.deliver_due(t + timedelta(hours=7 * step))
    with session_scope() as s:
        w = s.get(Webhook, hook["webhook_id"])
        assert w.status == "disabled" and "failed attempts" in w.disabled_reason
        assert not s.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == w.webhook_id, WebhookDelivery.status == "pending").count()
        assert max(d.attempts for d in s.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == w.webhook_id)) <= webhooks.MAX_ATTEMPTS
    assert mails == ["flaky@int.test"]
    assert webhooks.emit(ws, "research.failed", {"run_id": "r3"}) == []  # disabled: nothing queued
    r = secured.patch(f"/api/v1/webhooks/{hook['webhook_id']}", json={"status": "active"}).json()
    assert r["status"] == "active" and r["consecutive_failures"] == 0 and r["disabled_reason"] is None


def test_send_time_check_blocks_endpoints_that_now_resolve_privately(secured, receiver, monkeypatch):
    from atlas import webhooks

    got, _ = receiver
    acct = _signup(secured, "rebind@int.test", "Rebind")
    hook = secured.post("/api/v1/webhooks", json={"url": "https://hooks.example.test/r", "events": ["agent.fired"]}).json()
    monkeypatch.setattr(webhooks, "resolve", lambda host: ["127.0.0.1"])
    t = secured.post(f"/api/v1/webhooks/{hook['webhook_id']}/test").json()
    assert t["status"] == "failed" and "private" in t["error"] and got == []
    assert acct


def test_deleting_the_workspace_removes_its_webhooks(secured, receiver):
    from atlas.data.db import Webhook, WebhookDelivery, session_scope

    acct = _signup(secured, "leaving@int.test", "Leaving")
    hook = secured.post("/api/v1/webhooks", json={"url": "https://hooks.example.test/bye", "events": ["agent.fired"]}).json()
    secured.post(f"/api/v1/webhooks/{hook['webhook_id']}/test")
    r = secured.post("/api/v1/account/delete", json={"password": "correct horse battery", "confirm_email": "leaving@int.test"})
    assert r.status_code in (200, 204), r.text
    with session_scope() as s:
        assert s.get(Webhook, hook["webhook_id"]) is None
        assert not s.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == hook["webhook_id"]).count()
    assert acct
