"""Outbound webhooks: a workspace's alerts, agents and research results, sent to its own systems.

Each delivery is a JSON POST signed with the webhook's secret:

    X-Atlas-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256 of "<t>.<raw body>">

Receivers recompute the HMAC, compare in constant time and reject old timestamps. The payload's `id`
stays the same across retries, so receivers can drop duplicates. The `slack` format posts
`{"text": ...}` instead, for a Slack incoming-webhook URL.

Deliveries are attempted right away (in a background thread) and retried with backoff by the
scheduler: 1 min, 5 min, 30 min, 2 h, 6 h. Endpoints must be HTTPS on a public address (no
redirects followed); a webhook that keeps failing is disabled and its creator emailed.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, select, update

from atlas import secretbox
from atlas.auth import Principal, audit
from atlas.data.db import User, Webhook, WebhookDelivery, Workspace, session_scope, utcnow

log = logging.getLogger(__name__)

EVENTS = {
    "alert.triggered": "An alert's condition started holding",
    "agent.fired": "A monitoring agent fired and started research",
    "research.completed": "A research run finished",
    "research.failed": "A research run failed",
    "scenarios.changed": "A scenario scan found new or stronger scenarios",
}
FORMATS = ("atlas", "slack")
RETRY_DELAYS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6)]
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
DISABLE_AFTER = 15  # consecutive failed attempts
CLAIM = timedelta(minutes=2)  # a claimed attempt is not picked up again for this long
KEEP_DELIVERIES = timedelta(days=30)
TIMEOUT = 10.0

# Overridable in tests: the HTTP client, the resolver and whether emit() delivers in a thread.
client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=TIMEOUT, follow_redirects=False)  # noqa: E731
resolve: Callable[[str], list[str]] = lambda host: [ai[4][0] for ai in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]  # noqa: E731
BACKGROUND = True


class WebhookError(ValueError):
    pass


def _allow_private() -> bool:
    return os.getenv("ATLAS_WEBHOOKS_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes")


def check_url(url: str) -> str:
    """HTTPS to a public address only (no credentials in the URL). Raises WebhookError."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" and not (_allow_private() and parts.scheme == "http"):
        raise WebhookError("webhook URLs must use https")
    if not parts.hostname or parts.username or parts.password:
        raise WebhookError("webhook URL must name a host and carry no credentials")
    if len(url) > 2000:
        raise WebhookError("webhook URL is too long")
    if _allow_private():
        return url
    try:
        addresses = resolve(parts.hostname)
    except OSError as exc:
        raise WebhookError(f"cannot resolve {parts.hostname}") from exc
    for a in addresses:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise WebhookError(f"{parts.hostname} resolves to a private or reserved address")
    return url


def _check_events(events: list[str]) -> list[str]:
    unknown = [e for e in events if e not in EVENTS]
    if unknown or not events:
        raise WebhookError(f"choose at least one event from: {', '.join(EVENTS)}" + (f" (unknown: {', '.join(unknown)})" if unknown else ""))
    return sorted(set(events))


def _to_dict(w: Webhook) -> dict[str, Any]:
    iso = lambda d: d.isoformat(timespec="seconds") + "Z" if d else None  # noqa: E731
    return {"webhook_id": w.webhook_id, "url": w.url, "description": w.description, "events": w.events, "format": w.format,
            "status": w.status, "disabled_reason": w.disabled_reason, "consecutive_failures": w.consecutive_failures,
            "last_delivery_at": iso(w.last_delivery_at), "created_at": iso(w.created_at)}


def _new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def create(p: Principal, url: str, events: list[str], description: str = "", format: str = "atlas") -> dict[str, Any]:
    if format not in FORMATS:
        raise WebhookError(f"format must be one of {', '.join(FORMATS)}")
    url, events = check_url(url), _check_events(events)
    secret = _new_secret()
    with session_scope() as s:
        w = Webhook(workspace_id=p.workspace_id, user_id=p.user_id, url=url, events=events, description=description.strip()[:200],
                    format=format, secret_enc=secretbox.encrypt(secret))
        s.add(w)
        s.flush()
        out = _to_dict(w)
    audit("webhook.created", p.user_id, p.workspace_id, webhook_id=out["webhook_id"], host=urlsplit(url).hostname, events=events)
    return {**out, "secret": secret}  # shown once


def list_webhooks(workspace_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(select(Webhook).where(Webhook.workspace_id == workspace_id).order_by(Webhook.created_at)).scalars()
        return [_to_dict(w) for w in rows]


def _owned(s, webhook_id: str, workspace_id: str) -> Webhook:
    w = s.get(Webhook, webhook_id)
    if w is None or w.workspace_id != workspace_id:
        raise KeyError(webhook_id)
    return w


def update_webhook(p: Principal, webhook_id: str, *, events: list[str] | None = None, description: str | None = None,
                   status: str | None = None) -> dict[str, Any]:
    with session_scope() as s:
        w = _owned(s, webhook_id, p.workspace_id)
        if events is not None:
            w.events = _check_events(events)
        if description is not None:
            w.description = description.strip()[:200]
        if status is not None:
            if status not in ("active", "paused"):
                raise WebhookError("status must be active or paused")
            if status == "active":
                check_url(w.url)  # the address may have changed since
                w.disabled_reason, w.consecutive_failures = None, 0
            w.status = status
        out = _to_dict(w)
    audit("webhook.updated", p.user_id, p.workspace_id, webhook_id=webhook_id, status=out["status"], events=out["events"])
    return out


def rotate_secret(p: Principal, webhook_id: str) -> dict[str, Any]:
    secret = _new_secret()
    with session_scope() as s:
        w = _owned(s, webhook_id, p.workspace_id)
        w.secret_enc = secretbox.encrypt(secret)
    audit("webhook.secret_rotated", p.user_id, p.workspace_id, webhook_id=webhook_id)
    return {"webhook_id": webhook_id, "secret": secret}


def delete_webhook(p: Principal, webhook_id: str) -> None:
    with session_scope() as s:
        w = _owned(s, webhook_id, p.workspace_id)
        host = urlsplit(w.url).hostname
        s.execute(delete(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook_id))
        s.delete(w)
    audit("webhook.deleted", p.user_id, p.workspace_id, webhook_id=webhook_id, host=host)


def deliveries(workspace_id: str, webhook_id: str, limit: int = 50) -> list[dict[str, Any]]:
    iso = lambda d: d.isoformat(timespec="seconds") + "Z" if d else None  # noqa: E731
    with session_scope() as s:
        _owned(s, webhook_id, workspace_id)
        rows = s.execute(select(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook_id)
                         .order_by(WebhookDelivery.created_at.desc()).limit(limit)).scalars()
        return [{"delivery_id": d.delivery_id, "event": d.event, "status": d.status, "attempts": d.attempts,
                 "response_code": d.response_code, "error": d.error, "created_at": iso(d.created_at),
                 "delivered_at": iso(d.delivered_at), "next_attempt_at": iso(d.next_attempt_at) if d.status == "pending" else None}
                for d in rows]


# ---------------------------------------------------------------- sending


def public_url(path: str) -> str:
    return os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/") + path


def emit(workspace_id: str, event: str, data: dict[str, Any]) -> list[str]:
    """Queue `event` for every active webhook of the workspace subscribed to it; returns delivery ids."""
    assert event in EVENTS or event == "ping", event
    try:
        with session_scope() as s:
            ws = s.get(Workspace, workspace_id)
            if ws is None or ws.suspended_at is not None:
                return []
            hooks = [w.webhook_id for w in s.execute(select(Webhook).where(
                Webhook.workspace_id == workspace_id, Webhook.status == "active")).scalars() if event in (w.events or [])]
            ids = []
            for wid in hooks:
                d = WebhookDelivery(webhook_id=wid, event=event, payload={}, next_attempt_at=utcnow())
                s.add(d)
                s.flush()
                d.payload = {"id": d.delivery_id, "event": event, "created_at": utcnow().isoformat(timespec="seconds") + "Z",
                             "workspace_id": workspace_id, "data": data}
                ids.append(d.delivery_id)
    except Exception:  # never let a webhook problem break the alert, agent or research that emitted it
        log.exception("could not queue webhook event %s", event)
        return []
    if ids:
        if BACKGROUND:
            threading.Thread(target=_deliver_many, args=(ids,), daemon=True).start()
        else:
            _deliver_many(ids)
    return ids


def _deliver_many(ids: list[str]) -> None:
    for i in ids:
        try:
            attempt(i)
        except Exception:
            log.exception("webhook delivery %s failed unexpectedly", i)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def _slack_text(event: str, data: dict[str, Any]) -> str:
    link = data.get("research_url")
    if event == "alert.triggered":
        text = f"*ATLAS alert: {data.get('name')}*\n{data.get('message')}"
    elif event == "agent.fired":
        text = f"*ATLAS agent fired: {data.get('name')}*\n" + "\n".join(f"• {m}" for m in data.get("findings", [])[:6])
    elif event == "research.completed":
        text = f"*Research ready: {data.get('title') or data.get('query')}*"
    elif event == "scenarios.changed":
        text = "*Scenario agent: what changed*\n" + "\n".join(f"• {m}" for m in data.get("summary", [])[:8])
    elif event == "research.failed":
        text = f"*Research failed:* {data.get('query')}"
    else:
        text = "ATLAS test message: this webhook is connected."
    return text + (f"\n<{link}|Open in ATLAS>" if link else "")


def attempt(delivery_id: str, now=None) -> str:
    """Try one delivery if it is due and unclaimed. Returns its status afterwards."""
    now = now or utcnow()
    with session_scope() as s:
        claimed = s.execute(update(WebhookDelivery).where(
            WebhookDelivery.delivery_id == delivery_id, WebhookDelivery.status == "pending",
            WebhookDelivery.next_attempt_at <= now).values(next_attempt_at=now + CLAIM)).rowcount
        d = s.get(WebhookDelivery, delivery_id)
        if d is None:
            return "missing"
        if not claimed:
            return d.status
        w = s.get(Webhook, d.webhook_id)
        if w is None or (w.status != "active" and d.event != "ping"):  # a test ping also reaches a paused webhook
            d.status, d.error, d.next_attempt_at = "failed", "webhook is not active", None
            return d.status
        url, fmt, secret, event, payload = w.url, w.format, secretbox.decrypt(w.secret_enc), d.event, dict(d.payload)
    code, error = None, None
    if secret is None:
        error = "webhook secret cannot be decrypted (was ATLAS_SECRET_KEY changed?); rotate the secret"
    else:
        try:
            check_url(url)  # re-checked at send time: DNS may now point somewhere private
            body = json.dumps({"text": _slack_text(event, payload["data"])} if fmt == "slack" else payload,
                              separators=(",", ":"), default=str).encode()
            ts = int(time.time())
            headers = {"Content-Type": "application/json", "User-Agent": "ATLAS-Webhooks/1", "X-Atlas-Event": event,
                       "X-Atlas-Delivery": delivery_id, "X-Atlas-Signature": f"t={ts},v1={sign(secret, ts, body)}"}
            with client_factory() as c:
                r = c.post(url, content=body, headers=headers)
            code = r.status_code
            if not 200 <= code < 300:
                error = f"HTTP {code}"
        except WebhookError as exc:
            error = str(exc)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
    return _record(delivery_id, code, error)


def _record(delivery_id: str, code: int | None, error: str | None) -> str:
    disabled = None
    with session_scope() as s:
        d = s.get(WebhookDelivery, delivery_id)
        assert d is not None
        w = s.get(Webhook, d.webhook_id)
        d.attempts += 1
        d.response_code, d.error = code, error
        if w is not None:
            w.last_delivery_at = utcnow()
        if error is None:
            d.status, d.delivered_at, d.next_attempt_at = "delivered", utcnow(), None
            if w is not None:
                w.consecutive_failures = 0
        else:
            if d.attempts >= MAX_ATTEMPTS or d.event == "ping":
                d.status, d.next_attempt_at = "failed", None
            else:
                d.next_attempt_at = utcnow() + RETRY_DELAYS[d.attempts - 1]
            if w is not None and d.event != "ping":
                w.consecutive_failures += 1
                if w.consecutive_failures >= DISABLE_AFTER and w.status == "active":
                    w.status = "disabled"
                    w.disabled_reason = f"disabled after {w.consecutive_failures} failed attempts in a row (last: {error})"[:300]
                    disabled = (w.webhook_id, w.workspace_id, w.user_id, w.url)
        status = d.status
    if disabled:
        _on_disabled(*disabled)
    return status


def _on_disabled(webhook_id: str, workspace_id: str, user_id: str, url: str) -> None:
    from atlas import mailer

    audit("webhook.disabled", None, workspace_id, webhook_id=webhook_id, host=urlsplit(url).hostname)
    with session_scope() as s:
        user = s.get(User, user_id)
        to = user.email if user else None
        s.execute(update(WebhookDelivery).where(WebhookDelivery.webhook_id == webhook_id, WebhookDelivery.status == "pending")
                  .values(status="failed", error="webhook disabled", next_attempt_at=None))
    if to:
        try:
            mailer.send(to, "ATLAS webhook disabled", f"Deliveries to {url} kept failing, so ATLAS stopped sending to it.\n\n"
                        f"Fix the endpoint, then turn the webhook back on: {public_url('/account')}\n")
        except Exception:
            log.exception("could not email about disabled webhook %s", webhook_id)


def send_test(p: Principal, webhook_id: str) -> dict[str, Any]:
    """Send a `ping` now (even to a paused webhook) and report what happened."""
    with session_scope() as s:
        _owned(s, webhook_id, p.workspace_id)
        d = WebhookDelivery(webhook_id=webhook_id, event="ping", payload={}, next_attempt_at=utcnow())
        s.add(d)
        s.flush()
        d.payload = {"id": d.delivery_id, "event": "ping", "created_at": utcnow().isoformat(timespec="seconds") + "Z",
                     "workspace_id": p.workspace_id, "data": {"message": "ATLAS test delivery"}}
        did = d.delivery_id
    status = attempt(did)
    with session_scope() as s:
        d = s.get(WebhookDelivery, did)
        assert d is not None
        return {"delivery_id": did, "status": status, "response_code": d.response_code, "error": d.error}


def deliver_due(now=None) -> int:
    """Retry pending deliveries that are due (run by the scheduler); prunes old ones. Returns attempts made."""
    now = now or utcnow()
    from atlas.data.db import suspended_workspace_ids

    suspended = suspended_workspace_ids()
    with session_scope() as s:
        s.execute(delete(WebhookDelivery).where(WebhookDelivery.status != "pending", WebhookDelivery.created_at < now - KEEP_DELIVERIES))
        due = s.execute(select(WebhookDelivery.delivery_id, Webhook.workspace_id).join(Webhook, Webhook.webhook_id == WebhookDelivery.webhook_id)
                        .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= now)
                        .order_by(WebhookDelivery.next_attempt_at).limit(500)).all()
    n = 0
    for delivery_id, ws in due:
        if ws in suspended:
            continue
        attempt(delivery_id, now)
        n += 1
    return n


# ---------------------------------------------------------------- event payloads


def research_event(run_id: str) -> None:
    """Emit research.completed / research.failed for a finished run."""
    from atlas.data.db import ResearchRun

    with session_scope() as s:
        r = s.get(ResearchRun, run_id)
        if r is None or r.status not in ("complete", "failed"):
            return
        config, result = r.config or {}, r.result or {}
        data = {"run_id": r.run_id, "query": r.query, "status": r.status, "title": result.get("title"),
                "engine": r.engine, "verified": (result.get("verification") or {}).get("passed"),
                "trigger": "schedule" if config.get("schedule_id") else "agent" if config.get("agent_id") else "user",
                "research_url": public_url(f"/research?run={r.run_id}")}
        if r.status == "failed":
            data["error"] = (r.error or "")[:300]
        ws = r.workspace_id
    emit(ws, "research.completed" if data["status"] == "complete" else "research.failed", data)
