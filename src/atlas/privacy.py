"""Personal data rights: export (portability) and account deletion (erasure), plus ownership transfer.

Deleting an account removes the user, their credentials and memberships, and every workspace where
they are the only member together with all of its data (research, reports and their files, share
links, portfolios, alerts, agents, schedules, watchlist, invitations, keys, audit trail). Workspaces
shared with others keep their content; the person's audit entries there are de-identified. An
owner of a shared workspace must transfer ownership first, and a workspace with a live paid
subscription must cancel it first.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from atlas.auth import AuthError, Principal, ValidationError, _check_second_factor, audit, verify_password
from atlas.config import settings
from atlas.data.db import (
    AgentEvent,
    Alert,
    AlertEvent,
    ApiKey,
    AuditEvent,
    AuthSession,
    EmailVerification,
    Invitation,
    LoginChallenge,
    Membership,
    MonitorAgent,
    PasswordReset,
    Portfolio,
    Report,
    ReportSchedule,
    ReportShare,
    ResearchEvidence,
    ResearchRun,
    User,
    WatchlistItem,
    Workspace,
    session_scope,
)

LIVE_BILLING = {"active", "trialing", "past_due"}


def _iso(d) -> str | None:
    return d.isoformat(timespec="seconds") + "Z" if d else None


def transfer_ownership(principal: Principal, user_id: str) -> None:
    if principal.role != "owner":
        raise PermissionError("only the owner can transfer ownership")
    with session_scope() as s:
        target = s.get(Membership, (user_id, principal.workspace_id))
        me = s.get(Membership, (principal.user_id, principal.workspace_id))
        if target is None or user_id == principal.user_id:
            raise KeyError(user_id)
        target.role, me.role = "owner", "admin"  # type: ignore[union-attr]
    audit("team.ownership_transferred", principal.user_id, principal.workspace_id, member=user_id)


def export_personal_data(principal: Principal) -> dict[str, Any]:
    uid = principal.user_id
    with session_scope() as s:
        user = s.get(User, uid)
        if user is None:
            raise AuthError("account required")
        names = dict(s.execute(select(Workspace.workspace_id, Workspace.name)).all())

        def rows(model, *cols, where):
            return [dict(zip([c.key for c in cols], r)) for r in s.execute(select(*cols).where(where)).all()]

        data = {
            "account": {"user_id": uid, "email": user.email, "name": user.name, "created_at": _iso(user.created_at),
                        "last_login_at": _iso(user.last_login_at), "email_verified_at": _iso(user.email_verified_at),
                        "two_factor_enabled": bool(user.totp_enabled_at)},
            "workspaces": [{"workspace_id": m.workspace_id, "name": names.get(m.workspace_id), "role": m.role}
                           for m in s.execute(select(Membership).where(Membership.user_id == uid)).scalars()],
            "api_keys": [{"name": k.name, "prefix": k.prefix, "created_at": _iso(k.created_at), "last_used_at": _iso(k.last_used_at),
                          "revoked_at": _iso(k.revoked_at), "workspace": names.get(k.workspace_id)}
                         for k in s.execute(select(ApiKey).where(ApiKey.user_id == uid)).scalars()],
            "research": [{"run_id": r.run_id, "question": r.query, "status": r.status, "created_at": _iso(r.created_at),
                          "workspace": names.get(r.workspace_id), "title": (r.result or {}).get("title")}
                         for r in s.execute(select(ResearchRun).where(ResearchRun.user_id == uid, ResearchRun.status != "deleted")).scalars()],
            "portfolios": rows(Portfolio, Portfolio.name, Portfolio.holdings, where=Portfolio.user_id == uid),
            "alerts": rows(Alert, Alert.name, Alert.rule, Alert.status, where=Alert.user_id == uid),
            "agents": rows(MonitorAgent, MonitorAgent.name, MonitorAgent.triggers, MonitorAgent.status, where=MonitorAgent.user_id == uid),
            "schedules": rows(ReportSchedule, ReportSchedule.name, ReportSchedule.query, ReportSchedule.cadence, where=ReportSchedule.user_id == uid),
            "activity": [{"at": _iso(e.at), "action": e.action, "workspace": names.get(e.workspace_id), "detail": e.detail}
                         for e in s.execute(select(AuditEvent).where(AuditEvent.user_id == uid).order_by(AuditEvent.at)).scalars()],
        }
    audit("privacy.data_exported", uid, principal.workspace_id)
    return data


def _purge_workspace(s, ws_id: str) -> list[str]:
    """Delete everything a workspace owns; returns report ids whose files must be removed."""
    run_ids = list(s.execute(select(ResearchRun.run_id).where(ResearchRun.workspace_id == ws_id)).scalars())
    report_ids = list(s.execute(select(Report.report_id).where(Report.run_id.in_(run_ids))).scalars()) if run_ids else []
    agent_ids = list(s.execute(select(MonitorAgent.agent_id).where(MonitorAgent.workspace_id == ws_id)).scalars())
    alert_ids = list(s.execute(select(Alert.alert_id).where(Alert.workspace_id == ws_id)).scalars())
    if report_ids:
        s.execute(delete(ReportShare).where(ReportShare.report_id.in_(report_ids)))
        s.execute(delete(Report).where(Report.report_id.in_(report_ids)))
    if run_ids:
        s.execute(delete(ResearchEvidence).where(ResearchEvidence.run_id.in_(run_ids)))
        s.execute(update(ResearchRun).where(ResearchRun.parent_run_id.in_(run_ids)).values(parent_run_id=None))
        s.execute(delete(ResearchRun).where(ResearchRun.run_id.in_(run_ids)))
    if agent_ids:
        s.execute(delete(AgentEvent).where(AgentEvent.agent_id.in_(agent_ids)))
    if alert_ids:
        s.execute(delete(AlertEvent).where(AlertEvent.alert_id.in_(alert_ids)))
    for model in (MonitorAgent, Alert, ReportSchedule, Portfolio, WatchlistItem, Invitation, ApiKey, AuthSession,
                  ReportShare, Membership, AuditEvent):
        s.execute(delete(model).where(model.workspace_id == ws_id))
    s.execute(delete(Workspace).where(Workspace.workspace_id == ws_id))
    return report_ids


def deletion_blockers(user_id: str) -> tuple[list[str], list[str]]:
    """(problems that block deletion, names of workspaces that would be deleted)."""
    problems, purge = [], []
    with session_scope() as s:
        for m in s.execute(select(Membership).where(Membership.user_id == user_id)).scalars():
            ws = s.get(Workspace, m.workspace_id)
            members = s.execute(select(func.count()).select_from(Membership).where(Membership.workspace_id == m.workspace_id)).scalar()
            name = ws.name if ws else m.workspace_id
            if members == 1:
                if ws and ws.billing_status in LIVE_BILLING and ws.stripe_subscription_id:
                    problems.append(f"cancel the paid subscription of '{name}' first (Manage billing)")
                purge.append(name)
            elif m.role == "owner":
                problems.append(f"transfer ownership of '{name}' to another member first")
    return problems, purge


def delete_account(principal: Principal, password: str, code: str | None = None) -> dict[str, Any]:
    uid = principal.user_id
    with session_scope() as s:
        user = s.get(User, uid)
        if user is None:
            raise AuthError("account required")
        if not verify_password(password, user.password_hash):
            raise AuthError("password is incorrect")
        if user.totp_enabled_at and (not code or _check_second_factor(user, code) is None):
            raise AuthError("a valid two-factor code is required")
        email = user.email
    problems, purge_names = deletion_blockers(uid)
    if problems:
        raise ValidationError("; ".join(problems))
    with session_scope() as s:
        sole = [m.workspace_id for m in s.execute(select(Membership).where(Membership.user_id == uid)).scalars()
                if s.execute(select(func.count()).select_from(Membership).where(Membership.workspace_id == m.workspace_id)).scalar() == 1]
        report_ids: list[str] = []
        for ws_id in sole:
            report_ids += _purge_workspace(s, ws_id)
        for model in (LoginChallenge, PasswordReset, EmailVerification, AuthSession, ApiKey, Membership):
            s.execute(delete(model).where(model.user_id == uid))
        s.execute(delete(Invitation).where(Invitation.email == email))
        # Keep the shared workspaces' security trail, without identifying the person.
        for e in s.execute(select(AuditEvent).where(or_(AuditEvent.user_id == uid, AuditEvent.workspace_id.is_(None)))).scalars():
            if e.user_id == uid or email in str(e.detail or {}):
                e.user_id = None
                e.detail = {k: ("[deleted user]" if v == email or v == uid else v) for k, v in (e.detail or {}).items()}
        s.execute(delete(User).where(User.user_id == uid))
    for rid in report_ids:
        shutil.rmtree(Path(settings.object_store) / "reports" / rid, ignore_errors=True)
    audit("privacy.account_deleted", None, None, workspaces_deleted=len(sole))
    return {"deleted": True, "workspaces_deleted": purge_names}
