"""Accounts, workspaces, sessions, API keys and plan quotas (spec §15, §18).

Security notes:
- Passwords are hashed with scrypt (stdlib) and a per-user salt.
- Session tokens and API keys are random; only their SHA-256 hashes are stored.
- Every research run, report, alert and schedule belongs to a workspace, and every read
  is filtered by the caller's workspace. Market and macro data are shared.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from atlas.data.db import (
    LOCAL_WORKSPACE,
    Alert,
    ApiKey,
    AuditEvent,
    AuthSession,
    Membership,
    PasswordReset,
    ReportSchedule,
    ResearchRun,
    User,
    Workspace,
    session_scope,
    utcnow,
)

SESSION_TTL = timedelta(days=14)
RESET_TTL = timedelta(hours=1)
API_KEY_PREFIX = "atk_"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10

# Plan limits (spec §18 tiers). None = unlimited.
PLANS: dict[str, dict[str, int | None]] = {
    "explorer": {"research_per_day": 10, "llm_research_per_day": 3, "schedules": 1, "alerts": 5, "api_keys": 1},
    "investor": {"research_per_day": 50, "llm_research_per_day": 20, "schedules": 3, "alerts": 25, "api_keys": 2},
    "research_pro": {"research_per_day": 200, "llm_research_per_day": 60, "schedules": 10, "alerts": 100, "api_keys": 5},
    "pro": {"research_per_day": 1000, "llm_research_per_day": 300, "schedules": 50, "alerts": 500, "api_keys": 20},
}


class AuthError(Exception):
    """Bad credentials or token (HTTP 401)."""


class QuotaExceeded(Exception):
    """Plan limit reached (HTTP 429)."""


class ValidationError(ValueError):
    """Invalid input (HTTP 422)."""


@dataclass(frozen=True)
class Principal:
    user_id: str
    workspace_id: str
    role: str
    email: str
    via: str  # session | api_key | local


LOCAL_PRINCIPAL = Principal(user_id="local", workspace_id=LOCAL_WORKSPACE, role="owner", email="local", via="local")


def auth_required() -> bool:
    return os.getenv("ATLAS_AUTH", "required").lower() != "off"


def signup_allowed() -> bool:
    return os.getenv("ATLAS_ALLOW_SIGNUP", "true").lower() == "true"


# ---------------------------------------------------------------- hashing


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- login throttling

_attempts: dict[str, deque[float]] = defaultdict(deque)
_attempts_lock = threading.Lock()
MAX_ATTEMPTS, WINDOW_SECONDS = 10, 15 * 60


def _throttle(key: str) -> None:
    now = time.monotonic()
    with _attempts_lock:
        q = _attempts[key]
        while q and now - q[0] > WINDOW_SECONDS:
            q.popleft()
        if len(q) >= MAX_ATTEMPTS:
            raise AuthError("too many attempts; try again later")
        q.append(now)


def audit(action: str, user_id: str | None = None, workspace_id: str | None = None, **detail: Any) -> None:
    with session_scope() as s:
        s.add(AuditEvent(action=action, user_id=user_id, workspace_id=workspace_id, detail=detail))


# ---------------------------------------------------------------- accounts


def create_user(email: str, password: str, name: str = "", workspace_name: str | None = None,
                workspace_id: str | None = None, plan: str = "explorer") -> tuple[str, str]:
    """Create a user and either a new workspace or a membership in an existing one."""
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValidationError("invalid email address")
    if len(password) < MIN_PASSWORD:
        raise ValidationError(f"password must be at least {MIN_PASSWORD} characters")
    if plan not in PLANS:
        raise ValidationError(f"plan must be one of {sorted(PLANS)}")
    with session_scope() as s:
        if s.execute(select(User).where(User.email == email)).scalar_one_or_none():
            raise ValidationError("an account with this email already exists")
        user = User(email=email, name=name.strip(), password_hash=hash_password(password))
        s.add(user)
        s.flush()
        if workspace_id:
            if s.get(Workspace, workspace_id) is None:
                raise ValidationError(f"workspace {workspace_id} does not exist")
            role = "owner" if not s.execute(select(Membership).where(Membership.workspace_id == workspace_id)).first() else "member"
            ws_id = workspace_id
        else:
            ws = Workspace(name=workspace_name or f"{name or email.split('@')[0]}'s workspace", plan=plan)
            s.add(ws)
            s.flush()
            ws_id, role = ws.workspace_id, "owner"
        s.add(Membership(user_id=user.user_id, workspace_id=ws_id, role=role))
        uid = user.user_id
    audit("user.created", uid, ws_id, email=email)
    return uid, ws_id


def login(email: str, password: str, client: str = "") -> tuple[str, Principal]:
    email = email.strip().lower()
    _throttle(f"{email}|{client}")
    with session_scope() as s:
        user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
        # Verify against a dummy hash when the user is unknown, to keep timing similar.
        ok = verify_password(password, user.password_hash if user else hash_password("x" * MIN_PASSWORD))
        if not user or not ok:
            audit("login.failed", None, None, email=email)
            raise AuthError("invalid email or password")
        m = s.execute(select(Membership).where(Membership.user_id == user.user_id)).scalars().first()
        if m is None:
            raise AuthError("user has no workspace")
        token = secrets.token_urlsafe(32)
        s.add(AuthSession(token_hash=_token_hash(token), user_id=user.user_id, workspace_id=m.workspace_id,
                          expires_at=utcnow() + SESSION_TTL))
        user.last_login_at = utcnow()
        principal = Principal(user.user_id, m.workspace_id, m.role, user.email, "session")
    audit("login.succeeded", principal.user_id, principal.workspace_id)
    return token, principal


def logout(token: str) -> None:
    with session_scope() as s:
        sess = s.get(AuthSession, _token_hash(token))
        if sess:
            s.delete(sess)


def _principal(s, user_id: str, workspace_id: str, via: str) -> Principal:
    user = s.get(User, user_id)
    m = s.get(Membership, (user_id, workspace_id))
    if user is None or m is None:
        raise AuthError("account no longer has access to this workspace")
    return Principal(user_id, workspace_id, m.role, user.email, via)


def authenticate(token: str | None) -> Principal:
    """Resolve a session token or API key to a principal."""
    if not token:
        raise AuthError("authentication required")
    h = _token_hash(token)
    with session_scope() as s:
        if token.startswith(API_KEY_PREFIX):
            key = s.execute(select(ApiKey).where(ApiKey.key_hash == h)).scalar_one_or_none()
            if key is None or key.revoked_at is not None:
                raise AuthError("invalid or revoked API key")
            key.last_used_at = utcnow()
            return _principal(s, key.user_id, key.workspace_id, "api_key")
        sess = s.get(AuthSession, h)
        if sess is None or sess.expires_at < utcnow():
            raise AuthError("session expired; please sign in again")
        return _principal(s, sess.user_id, sess.workspace_id, "session")


def account(principal: Principal) -> dict[str, Any]:
    with session_scope() as s:
        ws = s.get(Workspace, principal.workspace_id)
        user = s.get(User, principal.user_id) if principal.via != "local" else None
        return {
            "user": {"user_id": principal.user_id, "email": principal.email, "name": user.name if user else "Local"},
            "workspace": {"workspace_id": ws.workspace_id, "name": ws.name, "plan": ws.plan} if ws else None,
            "role": principal.role,
            "auth": principal.via,
            "limits": PLANS.get(ws.plan if ws else "explorer"),
            "usage": usage(principal.workspace_id),
        }


# ---------------------------------------------------------------- passwords


def _set_password(s, user: User, new_password: str) -> None:
    if len(new_password) < MIN_PASSWORD:
        raise ValidationError(f"password must be at least {MIN_PASSWORD} characters")
    user.password_hash = hash_password(new_password)
    # Changing the password signs the user out everywhere.
    for sess in s.execute(select(AuthSession).where(AuthSession.user_id == user.user_id)).scalars():
        s.delete(sess)


def change_password(principal: Principal, current: str, new_password: str) -> None:
    with session_scope() as s:
        user = s.get(User, principal.user_id)
        if user is None or not verify_password(current, user.password_hash):
            raise AuthError("current password is incorrect")
        _set_password(s, user, new_password)
    audit("password.changed", principal.user_id, principal.workspace_id)


def request_password_reset(email: str, client: str = "") -> str | None:
    """Create a single-use reset token and email the link. Returns the token only for tests/CLI.

    Callers must respond identically whether or not the account exists (no enumeration).
    """
    from atlas import mailer

    email = email.strip().lower()
    _throttle(f"reset|{email}|{client}")
    with session_scope() as s:
        user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            return None
        token = secrets.token_urlsafe(32)
        s.add(PasswordReset(token_hash=_token_hash(token), user_id=user.user_id, expires_at=utcnow() + RESET_TTL))
        uid = user.user_id
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    mailer.send(email, "Reset your ATLAS password",
                f"Someone requested a password reset for your ATLAS account.\n\n"
                f"Reset it within {int(RESET_TTL.total_seconds() // 60)} minutes: {base}/reset?token={token}\n\n"
                f"If this wasn't you, ignore this email; your password is unchanged.")
    audit("password.reset_requested", uid, None)
    return token


def reset_password(token: str, new_password: str) -> None:
    with session_scope() as s:
        rec = s.get(PasswordReset, _token_hash(token or ""))
        if rec is None or rec.used_at is not None or rec.expires_at < utcnow():
            raise AuthError("reset link is invalid or has expired")
        user = s.get(User, rec.user_id)
        assert user is not None
        _set_password(s, user, new_password)
        rec.used_at = utcnow()
        uid = user.user_id
    audit("password.reset_completed", uid, None)


# ---------------------------------------------------------------- API keys


def create_api_key(principal: Principal, name: str) -> dict[str, Any]:
    check_quota(principal.workspace_id, "api_keys")
    token = API_KEY_PREFIX + secrets.token_urlsafe(32)
    with session_scope() as s:
        key = ApiKey(prefix=token[:10], key_hash=_token_hash(token), name=name.strip() or "API key",
                     user_id=principal.user_id, workspace_id=principal.workspace_id)
        s.add(key)
        s.flush()
        out = {"key_id": key.key_id, "name": key.name, "prefix": key.prefix, "key": token}
    audit("api_key.created", principal.user_id, principal.workspace_id, key_id=out["key_id"])
    return out


def list_api_keys(principal: Principal) -> list[dict[str, Any]]:
    iso = lambda d: d.isoformat(timespec="seconds") + "Z" if d else None  # noqa: E731
    with session_scope() as s:
        rows = s.execute(select(ApiKey).where(ApiKey.workspace_id == principal.workspace_id, ApiKey.revoked_at.is_(None))
                         .order_by(ApiKey.created_at.desc())).scalars().all()
        return [{"key_id": k.key_id, "name": k.name, "prefix": k.prefix, "created_at": iso(k.created_at),
                 "last_used_at": iso(k.last_used_at)} for k in rows]


def revoke_api_key(principal: Principal, key_id: str) -> bool:
    with session_scope() as s:
        key = s.get(ApiKey, key_id)
        if key is None or key.workspace_id != principal.workspace_id or key.revoked_at:
            return False
        key.revoked_at = utcnow()
    audit("api_key.revoked", principal.user_id, principal.workspace_id, key_id=key_id)
    return True


# ---------------------------------------------------------------- quotas


def usage(workspace_id: str) -> dict[str, int]:
    since = utcnow() - timedelta(days=1)
    with session_scope() as s:
        def count(q) -> int:
            return int(s.execute(q).scalar() or 0)

        return {
            "research_per_day": count(select(func.count()).select_from(ResearchRun).where(
                ResearchRun.workspace_id == workspace_id, ResearchRun.created_at >= since)),
            "llm_research_per_day": count(select(func.count()).select_from(ResearchRun).where(
                ResearchRun.workspace_id == workspace_id, ResearchRun.created_at >= since, ResearchRun.engine == "llm")),
            "schedules": count(select(func.count()).select_from(ReportSchedule).where(
                ReportSchedule.workspace_id == workspace_id, ReportSchedule.status != "deleted")),
            "alerts": count(select(func.count()).select_from(Alert).where(
                Alert.workspace_id == workspace_id, Alert.status != "deleted")),
            "api_keys": count(select(func.count()).select_from(ApiKey).where(
                ApiKey.workspace_id == workspace_id, ApiKey.revoked_at.is_(None))),
        }


def check_quota(workspace_id: str, resource: str) -> None:
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        plan = ws.plan if ws else "explorer"
    limit = PLANS.get(plan, PLANS["explorer"]).get(resource)
    if limit is None:
        return
    used = usage(workspace_id).get(resource, 0)
    if used >= limit:
        raise QuotaExceeded(f"{plan} plan limit reached for {resource.replace('_', ' ')} ({used}/{limit})")
