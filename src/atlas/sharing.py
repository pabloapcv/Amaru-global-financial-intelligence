"""Read-only report links for people outside a workspace.

A link carries a random 256-bit token; only its SHA-256 is stored. Links expire (1-90 days),
can be revoked, expose a single report (no workspace data or API access), and every view is
counted and audited without recording the token.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from atlas.auth import Principal, _token_hash, audit
from atlas.data.db import Report, ReportShare, ResearchRun, session_scope, utcnow

TOKEN_PREFIX = "shr_"
MAX_DAYS = 90


class ShareError(ValueError):
    pass


def _iso(d) -> str | None:
    return d.isoformat(timespec="seconds") + "Z" if d else None


def _row(sh: ReportShare) -> dict[str, Any]:
    now = utcnow()
    status = "revoked" if sh.revoked_at else "expired" if sh.expires_at <= now else "active"
    return {"share_id": sh.share_id, "report_id": sh.report_id, "prefix": sh.prefix, "status": status,
            "created_at": _iso(sh.created_at), "expires_at": _iso(sh.expires_at), "revoked_at": _iso(sh.revoked_at),
            "view_count": sh.view_count, "last_viewed_at": _iso(sh.last_viewed_at)}


def create(principal: Principal, report_id: str, days: int = 7) -> dict[str, Any]:
    if not 1 <= days <= MAX_DAYS:
        raise ShareError(f"links last 1-{MAX_DAYS} days")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with session_scope() as s:
        rep = s.get(Report, report_id)
        run = s.get(ResearchRun, rep.run_id) if rep else None
        if rep is None or run is None or run.workspace_id != principal.workspace_id:
            raise KeyError(report_id)
        sh = ReportShare(report_id=report_id, workspace_id=principal.workspace_id, created_by=principal.user_id,
                         token_hash=_token_hash(token), prefix=token[:10], expires_at=utcnow() + timedelta(days=days))
        s.add(sh)
        s.flush()
        out = {**_row(sh), "token": token, "path": f"/share/{token}"}
    audit("report.shared", principal.user_id, principal.workspace_id, report_id=report_id, share_id=out["share_id"], days=days)
    return out


def list_shares(principal: Principal, report_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(ReportShare).where(ReportShare.workspace_id == principal.workspace_id)
        if report_id:
            q = q.where(ReportShare.report_id == report_id)
        return [_row(sh) for sh in s.execute(q.order_by(ReportShare.created_at.desc())).scalars()]


def revoke(principal: Principal, share_id: str) -> bool:
    with session_scope() as s:
        sh = s.get(ReportShare, share_id)
        if sh is None or sh.workspace_id != principal.workspace_id or sh.revoked_at:
            return False
        sh.revoked_at = utcnow()
        report_id = sh.report_id
    audit("report.share_revoked", principal.user_id, principal.workspace_id, report_id=report_id, share_id=share_id)
    return True


def resolve(token: str) -> dict[str, Any] | None:
    """The report behind a valid (unexpired, unrevoked) token, counting the view; None otherwise."""
    if not token.startswith(TOKEN_PREFIX) or len(token) > 100:
        return None
    with session_scope() as s:
        sh = s.execute(select(ReportShare).where(ReportShare.token_hash == _token_hash(token))).scalar_one_or_none()
        if sh is None or sh.revoked_at or sh.expires_at <= utcnow():
            return None
        rep = s.get(Report, sh.report_id)
        if rep is None:
            return None
        sh.view_count = (sh.view_count or 0) + 1
        sh.last_viewed_at = utcnow()
        out = {"share_id": sh.share_id, "workspace_id": sh.workspace_id, "report_id": rep.report_id, "title": rep.title,
               "version": rep.version, "paths": dict(rep.formats), "expires_at": _iso(sh.expires_at)}
    audit("report.share_viewed", None, out["workspace_id"], report_id=out["report_id"], share_id=out["share_id"])
    return out
