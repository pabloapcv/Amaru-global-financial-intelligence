"""Workspace activity log (spec §15): the audit events of one workspace, for its owners and admins."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select

from atlas.data.db import AuditEvent, User, session_scope

CATEGORIES = {
    "signin": ("login.",),
    "team": ("team.", "user.created", "workspace."),
    "sharing": ("report.share",),
    "data": ("data.", "research."),
    "keys": ("api_key.",),
    "security": ("password.", "email.", "security.", "workspace.require_2fa"),
}
LABELS = {
    "login.succeeded": "Signed in", "team.invited": "Invited a person", "team.invitation_revoked": "Revoked an invitation",
    "team.joined": "Joined the workspace", "team.removed": "Removed a member", "team.left": "Left the workspace",
    "team.role_changed": "Changed a role", "user.created": "Account created", "workspace.switched": "Switched into this workspace",
    "workspace.plan_changed": "Plan changed", "report.shared": "Created a share link", "report.share_viewed": "Share link viewed",
    "report.share_revoked": "Revoked a share link", "data.exported": "Exported a dataset", "api_key.created": "Created an API key",
    "api_key.revoked": "Revoked an API key", "password.changed": "Changed password", "password.reset_requested": "Requested a password reset",
    "password.reset_completed": "Reset password", "email.verified": "Verified email", "email.verification_sent": "Verification email sent",
    "research.deleted": "Deleted research", "security.2fa_enabled": "Turned on two-factor", "security.2fa_disabled": "Turned off two-factor",
    "security.2fa_disabled_by_operator": "Two-factor reset by an operator", "login.2fa_failed": "Wrong two-factor code",
    "workspace.require_2fa_changed": "Changed the two-factor requirement",
}
MAX_LIMIT = 500


def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    at, _, event_id = cursor.partition("|")
    ts = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if ts.tzinfo is not None:  # stored times are naive UTC
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts, event_id


def events(workspace_id: str, *, category: str | None = None, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Newest first. `cursor` (from `next_cursor`) continues after the previous page without gaps,
    even when several events share a timestamp."""
    limit = max(1, min(limit, MAX_LIMIT))
    with session_scope() as s:
        q = select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
        if cursor:
            at, event_id = _parse_cursor(cursor)
            q = q.where(or_(AuditEvent.at < at, and_(AuditEvent.at == at, AuditEvent.event_id < event_id)))
        if category:
            q = q.where(or_(*[AuditEvent.action.like(f"{prefix}%") for prefix in CATEGORIES[category]]))
        rows = s.execute(q.order_by(AuditEvent.at.desc(), AuditEvent.event_id.desc()).limit(limit + 1)).scalars().all()
        more = len(rows) > limit
        rows = rows[:limit]
        ids = {r.user_id for r in rows if r.user_id} | {str(r.detail.get("member")) for r in rows if (r.detail or {}).get("member")}
        emails = dict(s.execute(select(User.user_id, User.email).where(User.user_id.in_(ids))).all()) if ids else {}
        out = []
        for r in rows:
            detail = dict(r.detail or {})
            if "member" in detail:
                detail["member"] = emails.get(detail["member"], detail["member"])
            out.append({"event_id": r.event_id, "at": r.at.isoformat(timespec="seconds") + "Z", "action": r.action,
                        "label": LABELS.get(r.action, r.action), "actor": emails.get(r.user_id) if r.user_id else None,
                        "detail": detail})
        last = rows[-1] if more and rows else None
        next_cursor = f"{last.at.isoformat()}|{last.event_id}" if last else None
    return {"events": out, "next_cursor": next_cursor}


def to_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at", "action", "label", "actor", "detail"])
    for e in rows:
        w.writerow([e["at"], e["action"], e["label"], e["actor"] or "", json.dumps(e["detail"], sort_keys=True)])
    return buf.getvalue()
