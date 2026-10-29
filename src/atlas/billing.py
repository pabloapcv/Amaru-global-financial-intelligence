"""Self-service billing with Stripe (Checkout, Customer Portal, webhooks).

Off until configured: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET and a price per paid plan
(STRIPE_PRICE_INVESTOR, STRIPE_PRICE_RESEARCH_PRO, STRIPE_PRICE_PRO). A workspace's plan changes
only through signed Stripe webhook events (never from the browser), each applied once.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Callable

import httpx
from sqlalchemy import select

from atlas.auth import PLANS, Principal, audit, set_plan
from atlas.data.db import StripeEvent, User, Workspace, session_scope

log = logging.getLogger("atlas.billing")

API = "https://api.stripe.com/v1"
PAID_PLANS = ("investor", "research_pro", "pro")
FREE_PLAN = "explorer"
SIGNATURE_TOLERANCE = 300  # seconds
ACTIVE = {"active", "trialing"}
ENDED = {"canceled", "unpaid", "incomplete_expired"}

# Tests swap this for a fake Stripe.
client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(  # noqa: E731
    base_url=API, auth=(os.getenv("STRIPE_SECRET_KEY", ""), ""), timeout=20)


class BillingError(Exception):
    pass


class NotConfigured(BillingError):
    pass


def prices() -> dict[str, str]:
    return {p: v for p in PAID_PLANS if (v := os.getenv(f"STRIPE_PRICE_{p.upper()}"))}


def configured() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY") and os.getenv("STRIPE_WEBHOOK_SECRET") and prices())


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: nested keys as a[b][c]."""
    out: list[tuple[str, str]] = []
    for k, v in data.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out += _flatten(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out += _flatten(item, f"{key}[{i}]") if isinstance(item, dict) else [(f"{key}[{i}]", str(item))]
        elif v is not None:
            out.append((key, str(v)))
    return out


def _post(path: str, data: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlencode

    with client_factory() as c:
        r = c.post(path, content=urlencode(_flatten(data)), headers={"content-type": "application/x-www-form-urlencoded"})
    if r.status_code >= 400:
        msg = (r.json().get("error", {}) or {}).get("message") if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
        log.warning("Stripe %s failed: %s", path, msg)
        raise BillingError(f"payment provider error: {msg}")
    return r.json()


def _require_owner(principal: Principal) -> None:
    if principal.role != "owner":
        raise PermissionError("only the workspace owner can manage billing")


def display_prices() -> dict[str, float]:
    """Monthly prices to show on the public pricing page: ATLAS_PLAN_PRICES="investor=49,research_pro=199,pro=799".
    Display only; what customers pay is set by the Stripe prices."""
    out: dict[str, float] = {}
    for part in os.getenv("ATLAS_PLAN_PRICES", "").split(","):
        key, _, value = part.partition("=")
        key = key.strip()
        try:
            amount = float(value)
        except ValueError:
            continue
        if key in PLANS and amount >= 0:
            out[key] = amount
    return out


def public_plans() -> dict[str, Any]:
    from atlas.auth import signup_allowed

    shown = display_prices()
    email = os.getenv("ATLAS_SALES_EMAIL", "").strip()
    return {"plans": [{"key": p, "limits": PLANS[p], "price_per_month": 0.0 if p == FREE_PLAN else shown.get(p)} for p in PLANS],
            "currency": os.getenv("ATLAS_PLAN_CURRENCY", "USD").upper()[:3], "signup_allowed": signup_allowed(),
            "self_service_billing": configured(), "sales_email": email if "@" in email else None, **legal_links()}


def legal_links() -> dict[str, str | None]:
    """Terms of service and privacy policy URLs (http/https only), shown at sign-up and on public pages."""
    out: dict[str, str | None] = {}
    for key, env in (("terms_url", "ATLAS_TERMS_URL"), ("privacy_url", "ATLAS_PRIVACY_URL")):
        v = os.getenv(env, "").strip()
        out[key] = v if v.startswith(("https://", "http://")) and len(v) < 500 else None
    return out


def summary(principal: Principal) -> dict[str, Any]:
    with session_scope() as s:
        ws = s.get(Workspace, principal.workspace_id)
        plan, status, customer = (ws.plan, ws.billing_status, ws.stripe_customer_id) if ws else (FREE_PLAN, None, None)
    available = prices()
    return {"configured": configured(), "plan": plan, "status": status, "has_customer": bool(customer),
            "can_manage": principal.role == "owner",
            "plans": [{"key": p, "limits": PLANS[p], "available": p in available} for p in PLANS]}


def checkout(principal: Principal, plan: str) -> str:
    """Start a Stripe Checkout session for a paid plan; returns its URL."""
    _require_owner(principal)
    if not configured():
        raise NotConfigured("billing is not set up on this deployment")
    price = prices().get(plan)
    if price is None:
        raise BillingError(f"plan '{plan}' cannot be bought here")
    with session_scope() as s:
        ws = s.get(Workspace, principal.workspace_id)
        user = s.get(User, principal.user_id)
        customer = ws.stripe_customer_id if ws else None
        email = user.email if user else None
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    meta = {"workspace_id": principal.workspace_id, "plan": plan}
    session = _post("/checkout/sessions", {
        "mode": "subscription", "line_items": [{"price": price, "quantity": 1}],
        "success_url": f"{base}/account?billing=success", "cancel_url": f"{base}/account?billing=cancelled",
        "client_reference_id": principal.workspace_id, "metadata": meta, "subscription_data": {"metadata": meta},
        **({"customer": customer} if customer else {"customer_email": email}),
    })
    audit("billing.checkout_started", principal.user_id, principal.workspace_id, plan=plan)
    return session["url"]


def portal(principal: Principal) -> str:
    """Stripe Customer Portal (payment method, invoices, cancel); returns its URL."""
    _require_owner(principal)
    if not configured():
        raise NotConfigured("billing is not set up on this deployment")
    with session_scope() as s:
        ws = s.get(Workspace, principal.workspace_id)
        customer = ws.stripe_customer_id if ws else None
    if not customer:
        raise BillingError("this workspace has no billing account yet")
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    return _post("/billing_portal/sessions", {"customer": customer, "return_url": f"{base}/account"})["url"]


def verify_signature(payload: bytes, header: str | None, secret: str, now: float | None = None) -> bool:
    """Stripe-Signature: t=<ts>,v1=<hex hmac-sha256 of "<ts>.<payload>">; rejects stale timestamps."""
    if not header or not secret:
        return False
    parts = [p.split("=", 1) for p in header.split(",") if "=" in p]
    ts = next((v for k, v in parts if k == "t"), None)
    sigs = [v for k, v in parts if k == "v1"]
    if not ts or not ts.isdigit() or not sigs:
        return False
    if abs((now if now is not None else time.time()) - int(ts)) > SIGNATURE_TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def _workspace_for(s, obj: dict[str, Any]) -> Workspace | None:
    ws_id = (obj.get("metadata") or {}).get("workspace_id") or obj.get("client_reference_id")
    ws = s.get(Workspace, ws_id) if ws_id else None
    if ws is None and obj.get("customer"):
        ws = s.execute(select(Workspace).where(Workspace.stripe_customer_id == obj["customer"])).scalar_one_or_none()
    return ws


def _plan_for_price(price_id: str | None) -> str | None:
    return next((p for p, v in prices().items() if v == price_id), None)


def handle_event(event: dict[str, Any]) -> str:
    """Apply a verified Stripe event once; returns what was done."""
    event_id, etype = str(event.get("id", "")), str(event.get("type", ""))
    obj = (event.get("data") or {}).get("object") or {}
    with session_scope() as s:
        if not event_id or s.get(StripeEvent, event_id):
            return "duplicate"
        s.add(StripeEvent(event_id=event_id, type=etype))
    try:
        return _apply(event_id, etype, obj)
    except Exception:
        with session_scope() as s:  # release the claim so Stripe's retry is processed
            row = s.get(StripeEvent, event_id)
            if row is not None:
                s.delete(row)
        raise


def _apply(event_id: str, etype: str, obj: dict[str, Any]) -> str:
    new_plan: str | None = None
    with session_scope() as s:
        ws = _workspace_for(s, obj)
        if ws is None:
            result = "ignored: no matching workspace"
        elif etype == "checkout.session.completed":
            ws.stripe_customer_id = obj.get("customer") or ws.stripe_customer_id
            ws.stripe_subscription_id = obj.get("subscription") or ws.stripe_subscription_id
            ws.billing_status = "active"
            plan = (obj.get("metadata") or {}).get("plan")
            new_plan = plan if plan in PAID_PLANS else None
            result = f"checkout completed ({plan})"
        elif etype in ("customer.subscription.created", "customer.subscription.updated"):
            ws.stripe_customer_id = obj.get("customer") or ws.stripe_customer_id
            ws.stripe_subscription_id = obj.get("id") or ws.stripe_subscription_id
            ws.billing_status = obj.get("status")
            items = ((obj.get("items") or {}).get("data") or [{}])
            price = (items[0].get("price") or {}).get("id") if items else None
            if obj.get("status") in ACTIVE:
                new_plan = _plan_for_price(price)
            elif obj.get("status") in ENDED:
                new_plan = FREE_PLAN
            result = f"subscription {obj.get('status')}"
        elif etype == "customer.subscription.deleted":
            ws.billing_status, ws.stripe_subscription_id = "canceled", None
            new_plan = FREE_PLAN
            result = "subscription ended"
        else:
            result = "ignored"
        ws_id = ws.workspace_id if ws else None
        current = ws.plan if ws else None
    if ws_id and new_plan and new_plan != current:
        set_plan(ws_id, new_plan)
        result += f"; plan {current} -> {new_plan}"
    with session_scope() as s:
        s.get(StripeEvent, event_id).result = result[:200]  # type: ignore[union-attr]
    if ws_id:
        audit("billing.webhook", None, ws_id, event=etype, result=result)
    return result
