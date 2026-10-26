"""Operator console: every workspace's plan, members, usage, Claude spend and billing status, plus plan
changes and suspension. Operators are listed in ATLAS_OPERATOR_EMAILS and must use two-factor
authentication. The console shows metadata and usage only, never research content; every action is
audited in the affected workspace.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from atlas.auth import PLANS, Principal, audit, llm_spend_this_month, set_plan
from atlas.data.db import AuditEvent, Membership, ResearchRun, User, Workspace, session_scope, utcnow


def _iso(d) -> str | None:
    return d.isoformat(timespec="seconds") + "Z" if d else None


def workspaces() -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=30)
    with session_scope() as s:
        members = {}
        for ws_id, email, role in s.execute(select(Membership.workspace_id, User.email, Membership.role)
                                            .join(User, User.user_id == Membership.user_id)).all():
            members.setdefault(ws_id, []).append((email, role))
        runs = dict(s.execute(select(ResearchRun.workspace_id, func.count()).where(ResearchRun.created_at >= since)
                              .group_by(ResearchRun.workspace_id)).all())
        active = dict(s.execute(select(AuditEvent.workspace_id, func.max(AuditEvent.at)).group_by(AuditEvent.workspace_id)).all())
        out = []
        for ws in s.execute(select(Workspace).order_by(Workspace.created_at.desc())).scalars():
            m = members.get(ws.workspace_id, [])
            out.append({
                "workspace_id": ws.workspace_id, "name": ws.name, "plan": ws.plan, "created_at": _iso(ws.created_at),
                "members": len(m), "owner": next((e for e, r in m if r == "owner"), None),
                "research_runs_30d": runs.get(ws.workspace_id, 0), "last_activity_at": _iso(active.get(ws.workspace_id)),
                "billing_status": ws.billing_status, "suspended_at": _iso(ws.suspended_at), "requires_2fa": bool(ws.require_2fa),
            })
    for w in out:
        w["claude_spend_month_usd"] = round(llm_spend_this_month(w["workspace_id"]), 2)
    return out


def overview() -> dict[str, Any]:
    from atlas.data.access import pipeline_status

    rows = workspaces()
    with session_scope() as s:
        users = s.execute(select(func.count()).select_from(User)).scalar() or 0
        runs_7d = s.execute(select(func.count()).select_from(ResearchRun)
                            .where(ResearchRun.created_at >= utcnow() - timedelta(days=7))).scalar() or 0
    pipe = pipeline_status(1)
    from atlas import feedback

    fb = feedback.summary()
    return {"workspaces": len(rows), "users": users, "research_runs_7d": runs_7d,
            "paying_workspaces": sum(1 for w in rows if w["billing_status"] in ("active", "trialing", "past_due")),
            "by_plan": {p: sum(1 for w in rows if w["plan"] == p) for p in PLANS},
            "claude_spend_month_usd": round(sum(w["claude_spend_month_usd"] for w in rows), 2),
            "suspended": sum(1 for w in rows if w["suspended_at"]),
            "pipeline_success_rate_30d": pipe.get("success_rate_30d"),
            "feedback_open": fb["open"], "satisfaction_30d": fb["satisfaction_30d"]}


def change_plan(operator: Principal, workspace_id: str, plan: str) -> None:
    set_plan(workspace_id, plan)
    audit("admin.plan_changed", operator.user_id, workspace_id, plan=plan, operator=operator.email)


def set_suspended(operator: Principal, workspace_id: str, suspended: bool, reason: str = "") -> None:
    if suspended and workspace_id == operator.workspace_id:
        raise ValueError("you cannot suspend the workspace you are signed in to")
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        if ws is None:
            raise KeyError(workspace_id)
        ws.suspended_at = utcnow() if suspended else None
    audit("admin.workspace_suspended" if suspended else "admin.workspace_reinstated", operator.user_id, workspace_id,
          reason=reason[:200], operator=operator.email)
