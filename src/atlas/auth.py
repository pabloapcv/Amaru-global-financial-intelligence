"""Accounts, workspaces, sessions, API keys and resource quotas (spec §15, §18).

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

from sqlalchemy import delete, func, select

from atlas.data.db import (
    LOCAL_WORKSPACE,
    Alert,
    ApiKey,
    AuditEvent,
    AuthSession,
    EmailVerification,
    Invitation,
    LoginChallenge,
    Membership,
    MonitorAgent,
    Newsletter,
    NewsletterSubscriber,
    PasswordReset,
    ReportSchedule,
    ResearchRun,
    User,
    Webhook,
    Workspace,
    session_scope,
    utcnow,
)

SESSION_TTL = timedelta(days=14)
RESET_TTL = timedelta(hours=1)
VERIFY_TTL = timedelta(hours=48)
API_KEY_PREFIX = "atk_"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10

# Named resource-limit sets (not paid tiers). None = unlimited.
PLANS: dict[str, dict[str, int | None]] = {
    "explorer": {"requests_per_minute": 60, "seats": 1, "llm_budget_usd_per_month": 2, "agents": 1, "research_per_day": 10, "llm_research_per_day": 3, "schedules": 1, "alerts": 5, "api_keys": 1, "webhooks": 1, "newsletter_subscribers": 100},
    "investor": {"requests_per_minute": 120, "seats": 2, "llm_budget_usd_per_month": 15, "agents": 3, "research_per_day": 50, "llm_research_per_day": 20, "schedules": 3, "alerts": 25, "api_keys": 2, "webhooks": 2, "newsletter_subscribers": 1000},
    "research_pro": {"requests_per_minute": 300, "seats": 5, "llm_budget_usd_per_month": 60, "agents": 10, "research_per_day": 200, "llm_research_per_day": 60, "schedules": 10, "alerts": 100, "api_keys": 5, "webhooks": 5, "newsletter_subscribers": 10000},
    "pro": {"requests_per_minute": 600, "seats": 25, "llm_budget_usd_per_month": 300, "agents": 50, "research_per_day": 1000, "llm_research_per_day": 300, "schedules": 50, "alerts": 500, "api_keys": 20, "webhooks": 20, "newsletter_subscribers": 100000},
}


class AuthError(Exception):
    """Bad credentials or token (HTTP 401)."""


class QuotaExceeded(Exception):
    """Plan limit reached (HTTP 429)."""


class ValidationError(ValueError):
    """Invalid input (HTTP 422)."""


class WorkspaceSuspended(Exception):
    """An operator suspended this workspace (HTTP 403)."""


def operator_emails() -> set[str]:
    return {e.strip().lower() for e in os.getenv("ATLAS_OPERATOR_EMAILS", "").split(",") if e.strip()}


def is_operator(email: str) -> bool:
    return email.strip().lower() in operator_emails()


class SecondFactorRequired(Exception):
    """Password accepted; a TOTP or recovery code is needed to finish signing in."""

    def __init__(self, challenge: str):
        super().__init__("two-factor code required")
        self.challenge = challenge


@dataclass(frozen=True)
class Principal:
    user_id: str
    workspace_id: str
    role: str
    email: str
    via: str  # session | api_key | local
    needs_2fa: bool = False  # the workspace requires two-factor authentication and this user has not set it up
    plan: str = "explorer"


LOCAL_PRINCIPAL = Principal(user_id="local", workspace_id=LOCAL_WORKSPACE, role="owner", email="local", via="local")


def auth_required() -> bool:
    return os.getenv("ATLAS_AUTH", "required").lower() != "off"


def verification_required() -> bool:
    return os.getenv("ATLAS_REQUIRE_EMAIL_VERIFICATION", "false").lower() == "true"


class VerificationRequired(Exception):
    """Email not verified while ATLAS_REQUIRE_EMAIL_VERIFICATION=true (HTTP 403)."""


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
                workspace_id: str | None = None, plan: str = "explorer", verified: bool = False,
                role: str | None = None) -> tuple[str, str]:
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
        user = User(email=email, name=name.strip(), password_hash=hash_password(password),
                    email_verified_at=utcnow() if verified else None)
        s.add(user)
        s.flush()
        if workspace_id:
            if s.get(Workspace, workspace_id) is None:
                raise ValidationError(f"workspace {workspace_id} does not exist")
            if not s.execute(select(Membership).where(Membership.workspace_id == workspace_id)).first():
                role = "owner"
            role = role or "member"
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
        challenge: str | None = None
        if user.totp_enabled_at:
            challenge = secrets.token_urlsafe(32)
            s.add(LoginChallenge(token_hash=_token_hash(challenge), user_id=user.user_id,
                                 expires_at=utcnow() + CHALLENGE_TTL))
            uid = user.user_id
        else:
            token, principal = _start_session(s, user)
    if challenge is not None:
        audit("login.password_ok_2fa_pending", uid, None)
        raise SecondFactorRequired(challenge)
    audit("login.succeeded", principal.user_id, principal.workspace_id)
    return token, principal


CHALLENGE_TTL = timedelta(minutes=5)
MAX_CHALLENGE_ATTEMPTS = 5
RECOVERY_CODES = 10


def _start_session(s, user: User) -> tuple[str, Principal]:
    m = s.execute(select(Membership).where(Membership.user_id == user.user_id)).scalars().first()
    if m is None:
        raise AuthError("user has no workspace")
    token = secrets.token_urlsafe(32)
    s.add(AuthSession(token_hash=_token_hash(token), user_id=user.user_id, workspace_id=m.workspace_id,
                      expires_at=utcnow() + SESSION_TTL))
    user.last_login_at = utcnow()
    return token, Principal(user.user_id, m.workspace_id, m.role, user.email, "session")


def _check_second_factor(user: User, code: str) -> str | None:
    """'totp' or 'recovery' when `code` is a valid unused code for `user` (consuming it), else None."""
    from atlas import secretbox, totp

    secret = secretbox.decrypt(user.totp_secret_enc) if user.totp_secret_enc else None
    if secret:
        step = totp.verify(secret, code, user.totp_last_step)
        if step is not None:
            user.totp_last_step = step
            return "totp"
    normalized = code.strip().lower().replace(" ", "").replace("-", "")
    hashes = list(user.recovery_codes or [])
    h = _token_hash(normalized)
    if h in hashes:
        hashes.remove(h)
        user.recovery_codes = hashes
        return "recovery"
    return None


def login_second_factor(challenge: str, code: str, client: str = "") -> tuple[str, Principal]:
    with session_scope() as s:
        ch = s.get(LoginChallenge, _token_hash(challenge))
        if ch is None or ch.used_at or ch.expires_at <= utcnow() or ch.attempts >= MAX_CHALLENGE_ATTEMPTS:
            raise AuthError("sign-in expired; enter your email and password again")
        _throttle(f"2fa|{ch.user_id}|{client}")
        ch.attempts += 1
        user = s.get(User, ch.user_id)
        assert user is not None
        method = _check_second_factor(user, code)
        if method is None:
            failed = True
        else:
            failed = False
            ch.used_at = utcnow()
            token, principal = _start_session(s, user)
            remaining = len(user.recovery_codes or [])
    if failed:
        audit("login.2fa_failed", ch.user_id, None)
        raise AuthError("invalid code")
    audit("login.succeeded", principal.user_id, principal.workspace_id, second_factor=method,
          **({"recovery_codes_left": remaining} if method == "recovery" else {}))
    return token, principal


def _recovery_codes() -> list[str]:
    import base64

    return [f"{c[:4]}-{c[4:8]}" for c in (base64.b32encode(secrets.token_bytes(5)).decode().lower() for _ in range(RECOVERY_CODES))]


def totp_begin(principal: Principal) -> dict[str, Any]:
    """Start enrollment: a new secret (pending until confirmed with a code)."""
    from atlas import secretbox, totp

    with session_scope() as s:
        user = s.get(User, principal.user_id)
        if user is None:
            raise AuthError("account required")
        if user.totp_enabled_at:
            raise ValidationError("two-factor authentication is already on")
        secret = totp.new_secret()
        user.totp_secret_enc, user.totp_last_step = secretbox.encrypt(secret), None
        email = user.email
    return {"secret": secret, "otpauth_uri": totp.provisioning_uri(secret, email)}


def totp_enable(principal: Principal, code: str, keep_session: str | None = None) -> list[str]:
    """Confirm enrollment with a current code; returns recovery codes (shown once)."""
    from atlas import secretbox, totp

    with session_scope() as s:
        user = s.get(User, principal.user_id)
        if user is None or user.totp_enabled_at or not user.totp_secret_enc:
            raise ValidationError("start enrollment first")
        secret = secretbox.decrypt(user.totp_secret_enc)
        step = totp.verify(secret, code) if secret else None
        if step is None:
            raise ValidationError("that code is not valid; check your device's clock and try again")
        codes = _recovery_codes()
        user.totp_enabled_at, user.totp_last_step = utcnow(), step
        user.recovery_codes = [_token_hash(c.replace("-", "")) for c in codes]
        # Other sessions were opened with the password alone: end them.
        others = delete(AuthSession).where(AuthSession.user_id == user.user_id)
        if keep_session:
            others = others.where(AuthSession.token_hash != _token_hash(keep_session))
        s.execute(others)
    audit("security.2fa_enabled", principal.user_id, principal.workspace_id)
    return codes


def totp_disable(principal: Principal, password: str, code: str) -> None:
    with session_scope() as s:
        user = s.get(User, principal.user_id)
        if user is None or not user.totp_enabled_at:
            raise ValidationError("two-factor authentication is not on")
        if not verify_password(password, user.password_hash) or _check_second_factor(user, code) is None:
            raise AuthError("password or code is incorrect")
        user.totp_enabled_at = user.totp_secret_enc = user.totp_last_step = user.recovery_codes = None
    audit("security.2fa_disabled", principal.user_id, principal.workspace_id)


def operator_disable_2fa(email: str) -> bool:
    """For an operator helping a user who lost their device and recovery codes (audited)."""
    with session_scope() as s:
        user = s.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
        if user is None or not user.totp_enabled_at:
            return False
        user.totp_enabled_at = user.totp_secret_enc = user.totp_last_step = user.recovery_codes = None
        s.execute(delete(AuthSession).where(AuthSession.user_id == user.user_id))
        uid = user.user_id
    audit("security.2fa_disabled_by_operator", uid, None)
    return True


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
    ws = s.get(Workspace, workspace_id)
    if ws is not None and ws.suspended_at is not None:
        raise WorkspaceSuspended(f"the workspace '{ws.name}' is suspended; contact support")
    needs = bool(ws and ws.require_2fa and not user.totp_enabled_at)
    return Principal(user_id, workspace_id, m.role, user.email, via, needs, ws.plan if ws else "explorer")


def set_require_2fa(principal: Principal, required: bool) -> None:
    """Owner-only workspace policy. The owner must use two-factor authentication before requiring it."""
    with session_scope() as s:
        if principal.role != "owner":
            raise AuthError("only the workspace owner can change this")
        user = s.get(User, principal.user_id)
        if required and not (user and user.totp_enabled_at):
            raise ValidationError("turn on two-factor authentication for your own account first")
        ws = s.get(Workspace, principal.workspace_id)
        assert ws is not None
        ws.require_2fa = required
    audit("workspace.require_2fa_changed", principal.user_id, principal.workspace_id, required=required)


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
            "email_verified": bool(user.email_verified_at) if user else True,
            "two_factor": bool(user.totp_enabled_at) if user else False,
            "workspace_requires_2fa": bool(ws.require_2fa) if ws else False,
            "operator": bool(user and is_operator(user.email)),
            "recovery_codes_left": len(user.recovery_codes or []) if user and user.totp_enabled_at else None,
            "verification_required": verification_required(),
            "limits": PLANS.get(ws.plan if ws else "explorer"),
            "usage": usage(principal.workspace_id),
            "workspaces": [{"workspace_id": w.workspace_id, "name": w.name, "role": m.role}
                           for m, w in s.execute(select(Membership, Workspace).join(Workspace, Workspace.workspace_id == Membership.workspace_id)
                                                 .where(Membership.user_id == principal.user_id).order_by(Workspace.name)).all()]
            if principal.via != "local" else [],
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
    mailer.send(email, "Reset your Amaru password",
                f"Someone requested a password reset for your Amaru account.\n\n"
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


# ---------------------------------------------------------------- operator tools


def list_workspaces() -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(select(Workspace).order_by(Workspace.created_at)).scalars().all()
        out = []
        for ws in rows:
            members = s.execute(select(User.email, Membership.role).join(Membership, Membership.user_id == User.user_id)
                                .where(Membership.workspace_id == ws.workspace_id)).all()
            out.append({"workspace_id": ws.workspace_id, "name": ws.name, "plan": ws.plan,
                        "members": [f"{e} ({r})" for e, r in members]})
    for w in out:
        w["claude_spend_month_usd"] = round(llm_spend_this_month(w["workspace_id"]), 2)
    return out


def set_plan(workspace_id: str, plan: str) -> None:
    if plan not in PLANS:
        raise ValidationError(f"plan must be one of {sorted(PLANS)}")
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        if ws is None:
            raise ValidationError(f"workspace {workspace_id} does not exist")
        old, ws.plan = ws.plan, plan
    audit("workspace.plan_changed", None, workspace_id, old=old, new=plan)


# ---------------------------------------------------------------- email verification


def send_verification(user_id: str) -> str | None:
    """Email a 48-hour verification link. Returns the token (for tests/CLI); None if already verified."""
    from atlas import mailer

    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None or user.email_verified_at is not None:
            return None
        token = secrets.token_urlsafe(32)
        s.add(EmailVerification(token_hash=_token_hash(token), user_id=user_id, expires_at=utcnow() + VERIFY_TTL))
        email = user.email
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    mailer.send(email, "Verify your Amaru email",
                f"Confirm this email address for your Amaru account: {base}/verify?token={token}\n\n"
                f"The link is valid for {int(VERIFY_TTL.total_seconds() // 3600)} hours.")
    audit("email.verification_sent", user_id, None)
    return token


def verify_email(token: str) -> None:
    with session_scope() as s:
        rec = s.get(EmailVerification, _token_hash(token or ""))
        if rec is None or rec.used_at is not None or rec.expires_at < utcnow():
            raise AuthError("verification link is invalid or has expired")
        user = s.get(User, rec.user_id)
        assert user is not None
        user.email_verified_at = user.email_verified_at or utcnow()
        rec.used_at = utcnow()
        uid = user.user_id
    audit("email.verified", uid, None)


def is_verified(principal: Principal) -> bool:
    if principal.via == "local":
        return True
    with session_scope() as s:
        user = s.get(User, principal.user_id)
        return bool(user and user.email_verified_at)


def require_verified(principal: Principal) -> None:
    if verification_required() and not is_verified(principal):
        raise VerificationRequired("verify your email address to use this feature (check your inbox or resend from Account)")


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
                ResearchRun.workspace_id == workspace_id, ResearchRun.created_at >= since, ResearchRun.cached_from.is_(None))),
            "llm_research_per_day": count(select(func.count()).select_from(ResearchRun).where(
                ResearchRun.workspace_id == workspace_id, ResearchRun.created_at >= since, ResearchRun.engine == "llm",
                ResearchRun.cached_from.is_(None))),
            "schedules": count(select(func.count()).select_from(ReportSchedule).where(
                ReportSchedule.workspace_id == workspace_id, ReportSchedule.status != "deleted")),
            "alerts": count(select(func.count()).select_from(Alert).where(
                Alert.workspace_id == workspace_id, Alert.status != "deleted")),
            "llm_budget_usd_per_month": round(llm_spend_this_month(workspace_id), 4),
            "agents": count(select(func.count()).select_from(MonitorAgent).where(
                MonitorAgent.workspace_id == workspace_id, MonitorAgent.status != "deleted")),
            "api_keys": count(select(func.count()).select_from(ApiKey).where(
                ApiKey.workspace_id == workspace_id, ApiKey.revoked_at.is_(None))),
            "webhooks": count(select(func.count()).select_from(Webhook).where(Webhook.workspace_id == workspace_id)),
            "newsletter_subscribers": count(select(func.count()).select_from(NewsletterSubscriber).join(
                Newsletter, Newsletter.newsletter_id == NewsletterSubscriber.newsletter_id).where(
                Newsletter.workspace_id == workspace_id, NewsletterSubscriber.status != "unsubscribed")),
            # Seats: members plus invitations still open.
            "seats": count(select(func.count()).select_from(Membership).where(Membership.workspace_id == workspace_id))
            + count(select(func.count()).select_from(Invitation).where(
                Invitation.workspace_id == workspace_id, Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None),
                Invitation.expires_at > utcnow())),
        }


def llm_spend_this_month(workspace_id: str) -> float:
    now = utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        return float(s.execute(select(func.coalesce(func.sum(ResearchRun.cost_usd), 0.0)).where(
            ResearchRun.workspace_id == workspace_id, ResearchRun.created_at >= start)).scalar() or 0.0)


def llm_budget(workspace_id: str) -> float | None:
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        plan = ws.plan if ws else "explorer"
    v = PLANS.get(plan, PLANS["explorer"]).get("llm_budget_usd_per_month")
    return None if v is None else float(v)


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
