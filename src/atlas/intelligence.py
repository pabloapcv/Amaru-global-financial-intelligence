"""Continuous monitoring: alert rules evaluated after each ingestion (spec FR-009, §7.1 step 07)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import select

from atlas.analytics import anomalies
from atlas.data.access import DataUnavailable, get_series
from atlas.data.db import Alert, AlertEvent, session_scope, utcnow
from atlas.instruments import get_dataset

log = logging.getLogger("atlas.intelligence")

RULE_TYPES = {
    "threshold": "Fires when the latest value crosses `value` using `op` ('>' or '<').",
    "pct_change": "Fires when the change over `window_days` exceeds `abs_pct` percent (absolute).",
    "anomaly": "Fires when the latest period change has |robust z| >= `z` (default 3.5).",
}


def validate_rule(rule: dict[str, Any]) -> None:
    t = rule.get("type")
    if t not in RULE_TYPES:
        raise ValueError(f"rule.type must be one of {sorted(RULE_TYPES)}")
    if not get_dataset(rule.get("dataset_id", "")):
        raise ValueError("rule.dataset_id must be a registered dataset")
    if t == "threshold" and (rule.get("op") not in (">", "<") or not isinstance(rule.get("value"), (int, float))):
        raise ValueError("threshold rules need op ('>'|'<') and numeric value")
    if t == "pct_change" and not isinstance(rule.get("abs_pct"), (int, float)):
        raise ValueError("pct_change rules need numeric abs_pct")


def evaluate_rule(rule: dict[str, Any]) -> str | None:
    """Return a trigger message, or None when the rule does not fire."""
    hit = evaluate_rule_keyed(rule)
    return hit[0] if hit else None


def evaluate_rule_keyed(rule: dict[str, Any]) -> tuple[str, str] | None:
    """(message, trigger key) when the condition holds. The key identifies what fired: a state rule
    (threshold, % move) has one key while it holds; an anomaly is keyed by its observation date."""
    ds = get_dataset(rule["dataset_id"])
    assert ds is not None
    series, meta = get_series(rule["dataset_id"])
    last, last_date = float(series.iloc[-1]), series.index[-1].date()
    t = rule["type"]
    if t == "threshold":
        hit = last > rule["value"] if rule["op"] == ">" else last < rule["value"]
        return (f"{ds.name} at {last:g} {ds.unit} on {last_date} is {rule['op']} {rule['value']:g}", "holds") if hit else None
    if t == "pct_change":
        window = int(rule.get("window_days", 30))
        past = series[series.index <= series.index[-1] - timedelta(days=window)]
        if past.empty or past.iloc[-1] == 0:
            return None
        pct = (last / float(past.iloc[-1]) - 1) * 100
        return (
            (f"{ds.name} moved {pct:+.2f}% over {window} days to {last:g} ({last_date})", "holds")
            if abs(pct) >= rule["abs_pct"] else None
        )
    if t == "anomaly":
        res = anomalies(series, unit=ds.unit, frequency=ds.frequency, threshold=float(rule.get("z", 3.5)))
        lt = res["latest"]
        return (
            (f"Unusual move in {ds.name} on {lt['date']}: {lt['change']:+.3f} {lt['change_unit']} (robust z {lt['robust_z']:+.2f})",
             f"date:{lt['date']}")
            if lt["is_anomalous"] else None
        )
    return None


def evaluate_alerts(changed_datasets: Iterable[str] | None = None, workspace_id: str | None = None) -> list[dict[str, Any]]:
    changed = set(changed_datasets) if changed_datasets is not None else None
    fired = []
    with session_scope() as s:
        q = select(Alert).where(Alert.status == "active")
        if workspace_id is not None:
            q = q.where(Alert.workspace_id == workspace_id)
        from atlas.data.db import suspended_workspace_ids

        suspended = suspended_workspace_ids()
        alerts = [a for a in s.execute(q).scalars().all() if a.workspace_id not in suspended]
        for a in alerts:
            if changed is not None and a.rule.get("dataset_id") not in changed:
                continue
            a.last_evaluated_at = utcnow()
            try:
                hit = evaluate_rule_keyed(a.rule)
            except (DataUnavailable, ValueError) as exc:
                log.warning("alert %s not evaluable: %s", a.alert_id, exc)
                continue
            if hit is None:
                a.last_trigger_key = None  # condition no longer holds: re-arm
                continue
            msg, key = hit
            if key == a.last_trigger_key:
                continue  # already fired for this (still holding / same observation)
            a.last_trigger_key = key
            a.last_triggered_at = utcnow()
            s.add(AlertEvent(alert_id=a.alert_id, message=msg, payload={"rule": a.rule}))
            fired.append({"alert_id": a.alert_id, "name": a.name, "message": msg,
                          "notify": bool(a.notify_email), "user_id": a.user_id, "workspace_id": a.workspace_id})
    from atlas import webhooks

    for f in fired:
        if f.pop("notify"):
            _email(f)
        f.pop("user_id")
        webhooks.emit(f.pop("workspace_id"), "alert.triggered", {"alert_id": f["alert_id"], "name": f["name"], "message": f["message"],
                                                                  "alerts_url": webhooks.public_url("/intelligence")})
    return fired


def _email(f: dict[str, Any]) -> None:
    import os

    from atlas import mailer
    from atlas.data.db import User

    with session_scope() as s:
        user = s.get(User, f["user_id"])
        to = user.email if user else None
    if not to:
        return
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    try:
        mailer.send(to, f"ATLAS alert: {f['name']}", f"{f['message']}\n\nManage alerts: {base}/intelligence\n"
                    "Reference data published with a lag; not a live quote. Not investment advice.")
    except Exception:
        log.exception("alert email for %s failed", f["alert_id"])
