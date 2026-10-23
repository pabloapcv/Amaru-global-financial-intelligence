import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from atlas import auth, billing

SECRET = "whsec_test_only"


def _signup(c, email, ws):
    r = c.post("/api/v1/auth/signup", json={"email": email, "password": "correct horse battery", "workspace_name": ws})
    assert r.status_code == 201, r.text
    return r.json()


def _signed(c, event: dict, *, secret: str = SECRET, ts: int | None = None, tamper: bool = False):
    payload = json.dumps(event).encode()
    t = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    body = payload + (b" " if tamper else b"")
    return c.post("/api/v1/billing/webhook", content=body, headers={"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"})


@pytest.fixture
def stripe(seeded, monkeypatch):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        calls.append((req.url.path, form, req.headers.get("authorization")))
        if req.url.path == "/v1/checkout/sessions":
            return httpx.Response(200, json={"id": "cs_test_1", "url": "https://checkout.stripe.test/cs_test_1"})
        if req.url.path == "/v1/billing_portal/sessions":
            return httpx.Response(200, json={"url": "https://billing.stripe.test/p_1"})
        return httpx.Response(404, json={"error": {"message": "no such route"}})

    monkeypatch.setattr(billing, "client_factory", lambda: httpx.Client(base_url=billing.API, auth=("sk_test_fake", ""),
                                                                       transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("ATLAS_AUTH", "required")
    from atlas.api.main import app

    with TestClient(app) as c:
        yield c, calls, monkeypatch


def test_billing_is_off_until_configured(stripe):
    c, calls, _ = stripe
    _signup(c, "owner@nobill.test", "No billing")
    summary = c.get("/api/v1/billing").json()
    assert summary["configured"] is False and summary["plan"] == "explorer"
    assert c.post("/api/v1/billing/checkout", json={"plan": "investor"}).status_code == 503
    assert calls == []


def test_checkout_webhooks_and_portal(stripe):
    c, calls, mp = stripe
    mp.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    mp.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    mp.setenv("STRIPE_PRICE_INVESTOR", "price_inv")
    mp.setenv("STRIPE_PRICE_RESEARCH_PRO", "price_rp")
    owner = _signup(c, "payer@bill.test", "Paying desk")
    ws = owner["workspace"]["workspace_id"]
    assert c.get("/api/v1/billing").json()["configured"] is True

    r = c.post("/api/v1/billing/checkout", json={"plan": "investor"})
    assert r.status_code == 200 and r.json()["url"] == "https://checkout.stripe.test/cs_test_1"
    path, form, authz = calls[-1]
    assert path == "/v1/checkout/sessions" and authz.startswith("Basic ")
    assert form["mode"] == "subscription" and form["line_items[0][price]"] == "price_inv"
    assert form["client_reference_id"] == ws and form["metadata[plan]"] == "investor"
    assert form["customer_email"] == "payer@bill.test"
    assert c.post("/api/v1/billing/checkout", json={"plan": "pro"}).status_code == 409  # no price configured
    assert c.post("/api/v1/billing/portal").status_code == 409  # no customer yet

    # Unsigned, tampered, stale or wrongly signed events are rejected and change nothing.
    done = {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": ws, "customer": "cus_1", "subscription": "sub_1", "metadata": {"workspace_id": ws, "plan": "investor"}}}}
    assert c.post("/api/v1/billing/webhook", content=json.dumps(done)).status_code == 400
    assert _signed(c, done, tamper=True).status_code == 400
    assert _signed(c, done, ts=int(time.time()) - 3600).status_code == 400
    assert _signed(c, done, secret="whsec_wrong").status_code == 400
    assert c.get("/api/v1/billing").json()["plan"] == "explorer"

    assert _signed(c, done).json()["result"].endswith("plan explorer -> investor")
    assert _signed(c, done).json()["result"] == "duplicate"  # Stripe retries are applied once
    b = c.get("/api/v1/billing").json()
    assert b["plan"] == "investor" and b["has_customer"] and b["status"] == "active"
    assert c.post("/api/v1/billing/portal").json()["url"] == "https://billing.stripe.test/p_1"
    assert calls[-1][1]["customer"] == "cus_1"

    # Upgrades follow the subscription's price; a cancellation returns to the free plan.
    upd = {"id": "evt_2", "type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_1", "customer": "cus_1", "status": "active", "metadata": {"workspace_id": ws},
        "items": {"data": [{"price": {"id": "price_rp"}}]}}}}
    _signed(c, upd)
    assert c.get("/api/v1/billing").json()["plan"] == "research_pro"
    past_due = {**upd, "id": "evt_3", "data": {"object": {**upd["data"]["object"], "status": "past_due"}}}
    _signed(c, past_due)
    b = c.get("/api/v1/billing").json()
    assert b["plan"] == "research_pro" and b["status"] == "past_due"  # grace period: keep the plan while Stripe retries
    gone = {"id": "evt_4", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1", "customer": "cus_1"}}}
    _signed(c, gone)
    b = c.get("/api/v1/billing").json()
    assert b["plan"] == "explorer" and b["status"] == "canceled"

    # Only the owner manages billing.
    auth.set_plan(ws, "investor")
    tok = c.post("/api/v1/team/invitations", json={"email": "member@bill.test"}).json()["link"].split("token=")[1]
    c.cookies.clear()
    c.post("/api/v1/auth/invitations/accept", json={"token": tok, "password": "member password!"})
    assert c.post("/api/v1/billing/checkout", json={"plan": "investor"}).status_code == 403
    assert c.post("/api/v1/billing/portal").status_code == 403
    assert c.get("/api/v1/billing").json()["can_manage"] is False


def test_failed_event_is_retried_not_dropped(stripe):
    c, _, mp = stripe
    mp.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    mp.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    mp.setenv("STRIPE_PRICE_INVESTOR", "price_inv")
    ws = _signup(c, "retry@bill.test", "Retry desk")["workspace"]["workspace_id"]
    event = {"id": "evt_retry", "type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": ws, "customer": "cus_r", "metadata": {"workspace_id": ws, "plan": "investor"}}}}
    real = billing.set_plan
    mp.setattr(billing, "set_plan", lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        _signed(c, event)
    mp.setattr(billing, "set_plan", real)
    assert _signed(c, event).json()["result"].endswith("plan explorer -> investor")
