"""Weekly email digest (opt-in per person and workspace).

Monday mornings the scheduler emails each opted-in member a plain-text summary of their workspace's
week: watchlist moves, scheduled events and U.S. data due, new official releases, USDA balance updates
and the alerts and agents that fired. Every email carries a signed one-click unsubscribe link.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import select

from atlas import secretbox
from atlas.data.db import AgentEvent, Alert, AlertEvent, Membership, MonitorAgent, User, Workspace, session_scope, utcnow

log = logging.getLogger("atlas.digest")

SEND_WEEKDAY, SEND_HOUR_UTC = 0, 7  # Mondays from 07:00 UTC
MIN_INTERVAL = timedelta(days=6)


def _week_move(dataset_id: str) -> str | None:
    from atlas.data.access import DataUnavailable, get_series, latest_quote
    from atlas.instruments import get_dataset

    ds = get_dataset(dataset_id)
    try:
        if ds and ds.frequency == "daily":
            s, meta = get_series(dataset_id, start=(utcnow() - timedelta(days=40)).date())
            past = s[s.index <= s.index[-1] - timedelta(days=7)]
            if past.empty:
                return None
            last, before = float(s.iloc[-1]), float(past.iloc[-1])
            if meta.unit == "percent":
                return f"{last:.2f}% ({(last - before) * 100:+.0f} bp over the week to {s.index[-1].date()})"
            return f"{last:,.4g} {meta.unit} ({(last / before - 1) * 100:+.2f}% over the week to {s.index[-1].date()})"
        q = latest_quote(dataset_id)
        chg = f", {q['change_pct']:+.2f}% vs {q['previous_date']}" if q.get("change_pct") is not None else ""
        return f"{q['value']:,.4g} {q['unit']} ({q['observation_date']}{chg})"
    except (DataUnavailable, KeyError, IndexError):
        return None


def compose(user_id: str, workspace_id: str) -> dict[str, str]:
    """{subject, body} for one member's weekly digest."""
    from atlas import briefing, watchlist
    from atlas.instruments import get_dataset

    since = utcnow() - timedelta(days=7)
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        user = s.get(User, user_id)
        name = ws.name if ws else "your workspace"
        alert_events = [(a.name, e.message) for e, a in s.execute(
            select(AlertEvent, Alert).join(Alert, Alert.alert_id == AlertEvent.alert_id)
            .where(Alert.workspace_id == workspace_id, Alert.status != "deleted", AlertEvent.triggered_at >= since)
            .order_by(AlertEvent.triggered_at.desc()).limit(10)).all()]
        agent_events = [(a.name, "; ".join(f.get("message", "") for f in (e.findings or [])[:2]))
                        for e, a in s.execute(select(AgentEvent, MonitorAgent).join(MonitorAgent, MonitorAgent.agent_id == AgentEvent.agent_id)
                                              .where(MonitorAgent.workspace_id == workspace_id, MonitorAgent.status != "deleted",
                                                     AgentEvent.at >= since,
                                                     AgentEvent.status == "fired").order_by(AgentEvent.at.desc()).limit(10)).all()]
    week = briefing.week(days=7)
    lines = [f"ATLAS weekly digest for {name}", f"Week to {utcnow().date().isoformat()}", ""]

    ids = watchlist.items(workspace_id)
    lines.append("WATCHLIST")
    if ids:
        for d in ids:
            ds = get_dataset(d)
            move = _week_move(d)
            lines.append(f"- {ds.name if ds else d}: {move or 'no data available'}")
    else:
        lines.append("- Your watchlist is empty. Choose Watch on any instrument in Markets to follow it here.")
    lines.append("")

    lines.append("THIS WEEK")
    for e in week["events"]:
        when = e["date"] + (f" to {e['end_date']}" if e.get("end_date") and e["end_date"] != e["date"] else "")
        lines.append(f"- {when}: {e['title']}")
    due = [d for d in week["series_due"] if d.get("next_period_due")]
    if due:
        lines.append("- U.S. data due: " + "; ".join(f"{d['name']} ({d['next_period_due']})" for d in due))
    if not week["events"] and not due:
        lines.append("- No scheduled events.")
    lines.append("")

    if week["releases"]:
        lines.append("OFFICIAL RELEASES (past 7 days)")
        lines += [f"- {r['published'] or 'undated'} {r['publisher']}: {r['title']}\n  {r['url']}" for r in week["releases"]]
        lines.append("")

    recent = [u for u in week["usda"] if u.get("largest_revision") or u["usda_release"] >= (utcnow() - timedelta(days=31)).strftime("%Y-%m")]
    if recent:
        lines.append("USDA SUPPLY AND DEMAND")
        for u in recent:
            rev = u.get("largest_revision")
            extra = f"; {rev['market_year']} {rev['attribute'].replace('_', ' ')} revised {rev['change_pct']:+.1f}%" if rev and rev.get("change_pct") is not None else ""
            lines.append(f"- {u['name']}: stocks-to-use {u['stocks_to_use_pct']}% for {u['market_year']} (report {u['usda_release']}){extra}")
        lines.append("")

    if alert_events or agent_events:
        lines.append("YOUR ALERTS AND AGENTS")
        lines += [f"- Alert '{n}': {m}" for n, m in alert_events]
        lines += [f"- Agent '{n}': {m}" for n, m in agent_events]
        lines.append("")

    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    api = os.getenv("ATLAS_API_PUBLIC_URL", os.getenv("NEXT_PUBLIC_API_URL", "http://localhost:8010")).rstrip("/")
    lines += [
        "Prices are public reference series published with a lag, not live quotes. Research and decision support only; not investment advice.",
        f"Open ATLAS: {base}/",
        f"Unsubscribe from this weekly email: {api}/digest/unsubscribe?u={quote(user_id)}&w={quote(workspace_id)}&s={secretbox.sign(f'{user_id}|{workspace_id}')}",
    ]
    return {"to": user.email if user else "", "subject": f"ATLAS weekly: {name}", "body": "\n".join(lines)}


def set_opt_in(user_id: str, workspace_id: str, on: bool) -> None:
    with session_scope() as s:
        m = s.get(Membership, (user_id, workspace_id))
        if m is None:
            raise KeyError(workspace_id)
        m.digest_weekly = on


def opted_in(user_id: str, workspace_id: str) -> bool:
    with session_scope() as s:
        m = s.get(Membership, (user_id, workspace_id))
        return bool(m and m.digest_weekly)


def unsubscribe(user_id: str, workspace_id: str, signature: str) -> bool:
    if not secretbox.verify_signature(f"{user_id}|{workspace_id}", signature):
        return False
    with session_scope() as s:
        m = s.get(Membership, (user_id, workspace_id))
        if m is None:
            return False
        m.digest_weekly = False
    return True


def send_due(now=None) -> int:
    """Send this week's digests (Mondays from 07:00 UTC, at most once per member per week)."""
    from atlas import mailer

    now = now or utcnow()
    if now.weekday() != SEND_WEEKDAY or now.hour < SEND_HOUR_UTC:
        return 0
    from atlas.data.db import suspended_workspace_ids

    suspended = suspended_workspace_ids()
    with session_scope() as s:
        due = [(m.user_id, m.workspace_id) for m in s.execute(select(Membership).where(Membership.digest_weekly.is_(True))).scalars()
               if (m.digest_sent_at is None or now - m.digest_sent_at >= MIN_INTERVAL) and m.workspace_id not in suspended]
    sent = 0
    for uid, ws in due:
        try:
            msg = compose(uid, ws)
            if msg["to"]:
                mailer.send(msg["to"], msg["subject"], msg["body"])
                sent += 1
        except Exception:
            log.exception("digest for %s in %s failed", uid, ws)
            continue
        with session_scope() as s:
            m = s.get(Membership, (uid, ws))
            if m:
                m.digest_sent_at = now
    return sent
