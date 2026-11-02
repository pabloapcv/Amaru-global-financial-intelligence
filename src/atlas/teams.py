"""Workspace teams: invitations, members and roles.

Owners and admins invite people by email with a role (admin or member). An invitation is a
single-use token (only its hash is stored) that expires after 7 days and counts against the plan's
seats while open. It is accepted by signing up with the invited email, or by signing in to an
account with that email. The owner cannot be removed or demoted; removing someone also ends
their sessions in the workspace and revokes their API keys for it.
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select

from atlas import mailer
from atlas.auth import (
    EMAIL_RE,
    AuthError,
    Principal,
    ValidationError,
    _token_hash,
    audit,
    check_quota,
)
from atlas.data.db import ApiKey, AuthSession, Invitation, Membership, User, Workspace, session_scope, utcnow

INVITE_PREFIX = "inv_"
INVITE_TTL = timedelta(days=7)
ROLES = ("admin", "member")
MANAGERS = ("owner", "admin")


class Forbidden(PermissionError):
    pass


def _iso(d) -> str | None:
    return d.isoformat(timespec="seconds") + "Z" if d else None


def _require_manager(principal: Principal) -> None:
    if principal.role not in MANAGERS:
        raise Forbidden("only workspace owners and admins can manage the team")


def members(principal: Principal) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.user_id)
                         .where(Membership.workspace_id == principal.workspace_id).order_by(User.email)).all()
        return [{"user_id": u.user_id, "email": u.email, "name": u.name, "role": role, "two_factor": bool(u.totp_enabled_at),
                 "last_login_at": _iso(u.last_login_at), "you": u.user_id == principal.user_id} for u, role in rows]


def invitations(principal: Principal) -> list[dict[str, Any]]:
    _require_manager(principal)
    now = utcnow()
    with session_scope() as s:
        rows = s.execute(select(Invitation).where(Invitation.workspace_id == principal.workspace_id,
                                                  Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None),
                                                  Invitation.expires_at > now).order_by(Invitation.created_at.desc())).scalars()
        return [{"invitation_id": i.invitation_id, "email": i.email, "role": i.role, "created_at": _iso(i.created_at),
                 "expires_at": _iso(i.expires_at)} for i in rows]


def invite(principal: Principal, email: str, role: str = "member") -> dict[str, Any]:
    _require_manager(principal)
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValidationError("invalid email address")
    if role not in ROLES:
        raise ValidationError(f"role must be one of {list(ROLES)}")
    if role == "admin" and principal.role != "owner":
        raise Forbidden("only the owner can invite admins")
    with session_scope() as s:
        already = s.execute(select(Membership).join(User, User.user_id == Membership.user_id).where(
            Membership.workspace_id == principal.workspace_id, User.email == email)).first()
        if already:
            raise ValidationError(f"{email} is already a member")
        # A new invitation to the same address replaces any open one.
        for old in s.execute(select(Invitation).where(Invitation.workspace_id == principal.workspace_id, Invitation.email == email,
                                                      Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))).scalars():
            old.revoked_at = utcnow()
    check_quota(principal.workspace_id, "seats")
    token = INVITE_PREFIX + secrets.token_urlsafe(32)
    with session_scope() as s:
        inv = Invitation(workspace_id=principal.workspace_id, email=email, role=role, token_hash=_token_hash(token),
                         invited_by=principal.user_id, expires_at=utcnow() + INVITE_TTL)
        s.add(inv)
        s.flush()
        ws_name = s.get(Workspace, principal.workspace_id).name  # type: ignore[union-attr]
        out = {"invitation_id": inv.invitation_id, "email": email, "role": role, "expires_at": _iso(inv.expires_at)}
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    link = f"{base}/join?token={token}"
    emailed = mailer.send(email, f"You're invited to {ws_name} on Amaru",
                          f"{principal.email} invited you to join the workspace \"{ws_name}\" on Amaru as {role}.\n\n"
                          f"Accept the invitation (valid for 7 days): {link}\n\nIf you did not expect this, ignore this email.")
    audit("team.invited", principal.user_id, principal.workspace_id, invitation_id=out["invitation_id"], email=email, role=role)
    return {**out, "link": link, "emailed": emailed}


def revoke_invitation(principal: Principal, invitation_id: str) -> bool:
    _require_manager(principal)
    with session_scope() as s:
        inv = s.get(Invitation, invitation_id)
        if inv is None or inv.workspace_id != principal.workspace_id or inv.accepted_at or inv.revoked_at:
            return False
        inv.revoked_at = utcnow()
    audit("team.invitation_revoked", principal.user_id, principal.workspace_id, invitation_id=invitation_id)
    return True


def _open_invitation(s, token: str) -> Invitation:
    if not token.startswith(INVITE_PREFIX) or len(token) > 100:
        raise AuthError("invalid or expired invitation")
    inv = s.execute(select(Invitation).where(Invitation.token_hash == _token_hash(token))).scalar_one_or_none()
    if inv is None or inv.accepted_at or inv.revoked_at or inv.expires_at <= utcnow():
        raise AuthError("invalid or expired invitation")
    return inv


def describe(token: str) -> dict[str, Any]:
    """Public summary for the join page: which workspace, which email, which role."""
    with session_scope() as s:
        inv = _open_invitation(s, token)
        ws = s.get(Workspace, inv.workspace_id)
        inviter = s.get(User, inv.invited_by)
        has_account = s.execute(select(User.user_id).where(User.email == inv.email)).first() is not None
        return {"workspace_name": ws.name if ws else "", "email": inv.email, "role": inv.role,
                "invited_by": inviter.email if inviter else None, "expires_at": _iso(inv.expires_at), "has_account": has_account}


def accept(token: str, user_id: str) -> str:
    """Add an existing user to the invitation's workspace; returns its workspace_id."""
    with session_scope() as s:
        inv = _open_invitation(s, token)
        user = s.get(User, user_id)
        if user is None or user.email != inv.email:
            raise AuthError(f"this invitation is for {inv.email}; sign in with that account")
        if s.get(Membership, (user_id, inv.workspace_id)) is None:
            s.add(Membership(user_id=user_id, workspace_id=inv.workspace_id, role=inv.role))
        inv.accepted_at = utcnow()
        ws_id, role = inv.workspace_id, inv.role
    audit("team.joined", user_id, ws_id, role=role)
    return ws_id


def signup_with_invitation(token: str, password: str, name: str) -> tuple[str, str]:
    """Create an account for the invited email directly in the workspace."""
    from atlas.auth import create_user

    with session_scope() as s:
        inv = _open_invitation(s, token)
        email, ws_id, role = inv.email, inv.workspace_id, inv.role
    uid, _ = create_user(email, password, name, workspace_id=ws_id, role=role)
    with session_scope() as s:
        s.execute(select(Invitation).where(Invitation.token_hash == _token_hash(token))).scalar_one().accepted_at = utcnow()
    audit("team.joined", uid, ws_id, role=role, new_account=True)
    return uid, ws_id


def set_role(principal: Principal, user_id: str, role: str) -> None:
    if principal.role != "owner":
        raise Forbidden("only the owner can change roles")
    if role not in ROLES:
        raise ValidationError(f"role must be one of {list(ROLES)}")
    with session_scope() as s:
        m = s.get(Membership, (user_id, principal.workspace_id))
        if m is None:
            raise KeyError(user_id)
        if m.role == "owner":
            raise Forbidden("the owner's role cannot be changed")
        m.role = role
    audit("team.role_changed", principal.user_id, principal.workspace_id, member=user_id, role=role)


def remove(principal: Principal, user_id: str) -> None:
    """Remove a member (or leave, when removing yourself). Ends their access to this workspace at once."""
    leaving = user_id == principal.user_id
    if not leaving:
        _require_manager(principal)
    with session_scope() as s:
        m = s.get(Membership, (user_id, principal.workspace_id))
        if m is None:
            raise KeyError(user_id)
        if m.role == "owner":
            raise Forbidden("the owner cannot be removed")
        if m.role == "admin" and principal.role != "owner" and not leaving:
            raise Forbidden("only the owner can remove an admin")
        s.delete(m)
        s.execute(delete(AuthSession).where(AuthSession.user_id == user_id, AuthSession.workspace_id == principal.workspace_id))
        for key in s.execute(select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.workspace_id == principal.workspace_id,
                                                  ApiKey.revoked_at.is_(None))).scalars():
            key.revoked_at = utcnow()
    audit("team.left" if leaving else "team.removed", principal.user_id, principal.workspace_id, member=user_id)


def switch_workspace(session_token: str, user_id: str, workspace_id: str) -> None:
    """Point a signed-in session at another workspace the user belongs to."""
    with session_scope() as s:
        if s.get(Membership, (user_id, workspace_id)) is None:
            raise Forbidden("not a member of that workspace")
        sess = s.get(AuthSession, _token_hash(session_token))
        if sess is None or sess.user_id != user_id:
            raise AuthError("session required")
        sess.workspace_id = workspace_id
    audit("workspace.switched", user_id, workspace_id)
