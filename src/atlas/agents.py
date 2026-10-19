"""Monitoring agents (spec §21 Layer 3, FR-017).

An agent watches a set of datasets with trigger conditions. After data refreshes (or on demand)
it evaluates the triggers; when any fires and the agent is outside its cooldown, it starts a
research run with its configured template and question, records an event with the evidence
that fired it, and optionally emails the workspace owner.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import select

from atlas import analytics
from atlas.data.access import DataUnavailable, get_positioning, get_series
from atlas.data.db import LOCAL_WORKSPACE, AgentEvent, MonitorAgent, session_scope, utcnow
from atlas.instruments import get_dataset, positioning_for

log = logging.getLogger("atlas.agents")

TRIGGERS: dict[str, dict[str, Any]] = {
    "anomaly": {"label": "Unusual move", "param": "z", "default": 3.5, "help": "latest change has |robust z| ≥ z"},
    "pct_move": {"label": "Large move", "param": "abs_pct", "default": 5.0, "help": "absolute % change over window_days ≥ abs_pct",
                 "extra": {"window_days": 30}},
    "crowded_positioning": {"label": "Crowded positioning", "param": "extreme", "default": 90.0,
                            "help": "CFTC COT index ≥ extreme or ≤ 100 − extreme (3-year range)"},
    "high_vol_regime": {"label": "High-volatility regime", "param": "probability", "default": 0.8,
                        "help": "Markov-switching probability of the high-volatility regime ≥ probability"},
    "official_release": {"label": "Official release", "param": "keywords", "default": "FOMC statement", "kind": "text",
                         "help": "a new Fed, BEA or CFTC press release mentions any of these comma-separated keywords"},
}
RELEASE_TRIGGERS = {"official_release"}
MANUAL_RELEASE_LOOKBACK = timedelta(days=7)
MAX_WATCH = 20


class AgentError(ValueError):
    pass


def validate(watch: list[str], triggers: list[dict[str, Any]], action: dict[str, Any]) -> None:
    from atlas.research.templates import TEMPLATES

    if not triggers:
        raise AgentError("add at least one trigger")
    market_triggers = [t for t in triggers if t.get("type") not in RELEASE_TRIGGERS]
    if (market_triggers and not watch) or len(watch) > MAX_WATCH:
        raise AgentError(f"watch 1–{MAX_WATCH} datasets")
    for d in watch:
        ds = get_dataset(d)
        if ds is None or ds.kind != "market":
            raise AgentError(f"'{d}' is not a market instrument")
    for t in triggers:
        spec = TRIGGERS.get(t.get("type", ""))
        if spec is None:
            raise AgentError(f"trigger type must be one of {sorted(TRIGGERS)}")
        value = t.get(spec["param"], spec["default"])
        if spec.get("kind") == "text":
            words = [w for w in str(value).split(",") if w.strip()] if isinstance(value, str) else []
            if not words or len(words) > 10 or any(len(w.strip()) < 2 for w in words):
                raise AgentError(f"{t['type']}.{spec['param']}: 1–10 comma-separated keywords")
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            raise AgentError(f"{t['type']}.{spec['param']} must be a number")
    if action.get("template", "research_report") not in TEMPLATES:
        raise AgentError("unknown report template")
    if len(str(action.get("query", ""))) < 3:
        raise AgentError("action.query (the research question to run) is required")


def _evaluate_trigger(trigger: dict[str, Any], dsid: str) -> dict[str, Any] | None:
    """Return a finding dict when the trigger fires for dataset `dsid`, else None."""
    ds = get_dataset(dsid)
    assert ds is not None
    spec = TRIGGERS[trigger["type"]]
    level = float(trigger.get(spec["param"], spec["default"]))
    t = trigger["type"]
    if t == "crowded_positioning":
        cot = positioning_for(dsid)
        if not cot:
            return None
        df, _ = get_positioning(cot)
        st = analytics.positioning_stats(df, lookback_years=3)
        idx = st["cot_index"]
        if idx is not None and (idx >= level or idx <= 100 - level):
            return {"dataset_id": dsid, "trigger": t, "value": idx, "threshold": level,
                    "message": f"{ds.name}: speculators {st['positioning_extreme'] or 'at an extreme'} (COT index {idx}, report {st['report_date']})"}
        return None
    series, _ = get_series(dsid)
    if t == "anomaly":
        res = analytics.anomalies(series, unit=ds.unit, frequency=ds.frequency, threshold=level)
        lt = res["latest"]
        if lt["is_anomalous"]:
            return {"dataset_id": dsid, "trigger": t, "value": lt["robust_z"], "threshold": level,
                    "message": f"{ds.name}: unusual move {lt['change']:+.2f} {lt['change_unit']} on {lt['date']} (robust z {lt['robust_z']:+.2f})"}
    elif t == "pct_move":
        window = int(trigger.get("window_days", 30))
        past = series[series.index <= series.index[-1] - timedelta(days=window)]
        if not past.empty and past.iloc[-1]:
            pct = (float(series.iloc[-1]) / float(past.iloc[-1]) - 1) * 100
            if abs(pct) >= level:
                return {"dataset_id": dsid, "trigger": t, "value": round(pct, 2), "threshold": level,
                        "message": f"{ds.name}: {pct:+.2f}% over {window} days to {series.index[-1].date()}"}
    elif t == "high_vol_regime":
        res = analytics.volatility_regime(series, unit=ds.unit, frequency=ds.frequency)
        p = res["markov_high_vol_probability_now"]
        if p is not None and p >= level:
            return {"dataset_id": dsid, "trigger": t, "value": p, "threshold": level,
                    "message": f"{ds.name}: high-volatility regime probability {p:.2f} (realized vol {res['realized_vol_regime']})"}
    return None


def _release_findings(trigger: dict[str, Any], since, by_published: bool = False) -> list[dict[str, Any]]:
    from atlas import documents

    keywords = str(trigger.get("keywords", TRIGGERS["official_release"]["default"])).split(",")
    return [{"trigger": "official_release", "url": d["url"], "source": d["source"],
             "message": f"{d['publisher']} ({d['published'] or 'undated'}): {d['title']}"}
            for d in documents.new_since(since, keywords, trigger.get("sources"), by_published=by_published)]


def check_agent(agent_id: str, *, force: bool = False, background: bool = True, manual: bool = False) -> dict[str, Any]:
    """Evaluate one agent. Starts research if a trigger fires and the cooldown has passed (or force).
    `manual` (a user's "Check now") makes release triggers look at the past week's releases."""
    from atlas.research import engine

    with session_scope() as s:
        a = s.get(MonitorAgent, agent_id)
        if a is None:
            raise KeyError(agent_id)
        watch, triggers, action = list(a.watch), list(a.triggers), dict(a.action)
        ws, uid, cooldown, last_fired = a.workspace_id, a.user_id, a.cooldown_hours, a.last_fired_at
        name, notify = a.name, a.notify_email
        now = utcnow()
        since = a.last_checked_at or a.created_at
        a.last_checked_at = now
    findings, errors = [], []
    for trig in triggers:
        if trig["type"] in RELEASE_TRIGGERS:
            # Automatic checks: releases indexed since the last check. A manual check: the past week's releases.
            findings += _release_findings(trig, now - MANUAL_RELEASE_LOOKBACK, by_published=True) if manual \
                else _release_findings(trig, since)
    for dsid in watch:
        for trig in triggers:
            if trig["type"] in RELEASE_TRIGGERS:
                continue
            try:
                f = _evaluate_trigger(trig, dsid)
            except (DataUnavailable, ValueError) as exc:
                errors.append({"dataset_id": dsid, "trigger": trig["type"], "error": str(exc)[:200]})
                continue
            if f:
                findings.append(f)
    if not findings:
        return {"agent_id": agent_id, "fired": False, "findings": [], "errors": errors}

    in_cooldown = last_fired is not None and utcnow() - last_fired < timedelta(hours=cooldown)
    if in_cooldown and not force:
        with session_scope() as s:
            s.add(AgentEvent(agent_id=agent_id, findings=findings, status="suppressed"))
        return {"agent_id": agent_id, "fired": False, "suppressed": True, "findings": findings, "errors": errors}

    context = "; ".join(f["message"] for f in findings[:6])
    query = f"{action['query']} Context: monitoring agent '{name}' fired on: {context}."
    config = {"engine": action.get("engine", "auto"), "depth": action.get("depth", "standard"),
              "template": action.get("template", "research_report"), "formats": ["html", "xlsx"],
              "assets": [f["dataset_id"] for f in findings if f.get("dataset_id")][:4] or watch[:4], "agent_id": agent_id}
    release_kw = [str(t.get("keywords", "")) for t in triggers if t["type"] in RELEASE_TRIGGERS]
    if any(f["trigger"] in RELEASE_TRIGGERS for f in findings) and release_kw:
        config["documents_query"] = " ".join(k.replace(",", " ") for k in release_kw)
    run_id = engine.submit(query, config, uid, ws) if background else engine.create_run(query, config, uid, ws)
    with session_scope() as s:
        s.add(AgentEvent(agent_id=agent_id, findings=findings, run_id=run_id, status="fired"))
        a = s.get(MonitorAgent, agent_id)
        assert a is not None
        a.last_fired_at = utcnow()
    if not background:
        engine.execute_run(run_id)
    if notify:
        _notify(ws, name, findings, run_id)
    return {"agent_id": agent_id, "fired": True, "run_id": run_id, "findings": findings, "errors": errors}


def _notify(workspace_id: str, agent_name: str, findings: list[dict[str, Any]], run_id: str) -> None:
    import os

    from atlas import mailer
    from atlas.data.db import Membership, User

    with session_scope() as s:
        owners = s.execute(select(User.email).join(Membership, Membership.user_id == User.user_id).where(
            Membership.workspace_id == workspace_id, Membership.role == "owner")).scalars().all()
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    body = "\n".join(f"- {f['message']}" for f in findings) + f"\n\nResearch report: {base}/research?run={run_id}\n"
    for email in owners:
        try:
            mailer.send(email, f"ATLAS agent fired: {agent_name}", body)
        except Exception:
            log.exception("agent notification to %s failed", email)


def evaluate_agents(changed_datasets: Iterable[str] | None = None, workspace_id: str | None = None) -> list[dict[str, Any]]:
    """Check active agents that watch any changed dataset (or all agents when None)."""
    changed = set(changed_datasets) if changed_datasets is not None else None
    with session_scope() as s:
        q = select(MonitorAgent).where(MonitorAgent.status == "active")
        if workspace_id is not None:
            q = q.where(MonitorAgent.workspace_id == workspace_id)
        agents = [(a.agent_id, list(a.watch)) for a in s.execute(q).scalars()]
    results = []
    for agent_id, watch in agents:
        if changed is not None and not changed.intersection(watch):
            continue
        try:
            results.append(check_agent(agent_id))
        except Exception:
            log.exception("agent %s failed", agent_id)
    return results


def evaluate_release_agents() -> list[dict[str, Any]]:
    """Check active agents with an official-release trigger (after the document index gains releases)."""
    with session_scope() as s:
        ids = [a.agent_id for a in s.execute(select(MonitorAgent).where(MonitorAgent.status == "active")).scalars()
               if any(t.get("type") in RELEASE_TRIGGERS for t in a.triggers)]
    results = []
    for agent_id in ids:
        try:
            results.append(check_agent(agent_id))
        except Exception:
            log.exception("agent %s failed", agent_id)
    return results


# ---------------------------------------------------------------- CRUD


def _to_dict(a: MonitorAgent, events: list[AgentEvent] | None = None) -> dict[str, Any]:
    iso = lambda d: d.isoformat(timespec="seconds") + "Z" if d else None  # noqa: E731
    return {
        "agent_id": a.agent_id, "name": a.name, "watch": a.watch, "triggers": a.triggers, "action": a.action,
        "cooldown_hours": a.cooldown_hours, "notify_email": a.notify_email, "status": a.status,
        "last_checked_at": iso(a.last_checked_at), "last_fired_at": iso(a.last_fired_at),
        "events": [{"event_id": e.event_id, "at": iso(e.at), "status": e.status, "run_id": e.run_id,
                    "findings": e.findings, "read": e.read} for e in (events or [])],
    }


def create(name: str, watch: list[str], triggers: list[dict[str, Any]], action: dict[str, Any], cooldown_hours: int = 24,
           notify_email: bool = False, workspace_id: str = LOCAL_WORKSPACE, user_id: str = "local") -> dict[str, Any]:
    validate(watch, triggers, action)
    with session_scope() as s:
        a = MonitorAgent(name=name, watch=watch, triggers=triggers, action=action, cooldown_hours=cooldown_hours,
                         notify_email=notify_email, workspace_id=workspace_id, user_id=user_id)
        s.add(a)
        s.flush()
        return _to_dict(a)


def list_agents(workspace_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        agents = s.execute(select(MonitorAgent).where(MonitorAgent.workspace_id == workspace_id, MonitorAgent.status != "deleted")
                           .order_by(MonitorAgent.created_at.desc())).scalars().all()
        out = []
        for a in agents:
            events = s.execute(select(AgentEvent).where(AgentEvent.agent_id == a.agent_id)
                               .order_by(AgentEvent.at.desc()).limit(10)).scalars().all()
            out.append(_to_dict(a, events))
        return out


def get_owned(agent_id: str, workspace_id: str) -> bool:
    with session_scope() as s:
        a = s.get(MonitorAgent, agent_id)
        return a is not None and a.workspace_id == workspace_id and a.status != "deleted"


def set_status(agent_id: str, workspace_id: str, status: str) -> bool:
    with session_scope() as s:
        a = s.get(MonitorAgent, agent_id)
        if a is None or a.workspace_id != workspace_id:
            return False
        a.status = status
        return True


def unread_count(workspace_id: str) -> int:
    from sqlalchemy import func

    with session_scope() as s:
        return int(s.execute(select(func.count()).select_from(AgentEvent).join(MonitorAgent).where(
            MonitorAgent.workspace_id == workspace_id, AgentEvent.read.is_(False), AgentEvent.status == "fired")).scalar() or 0)


def mark_read(workspace_id: str) -> None:
    with session_scope() as s:
        ids = s.execute(select(AgentEvent.event_id).join(MonitorAgent).where(MonitorAgent.workspace_id == workspace_id)).scalars().all()
        for e in s.execute(select(AgentEvent).where(AgentEvent.event_id.in_(ids))).scalars():
            e.read = True
