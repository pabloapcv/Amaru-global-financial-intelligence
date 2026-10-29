"""ATLAS REST API (spec §14). Run with `atlas serve` or `uvicorn atlas.api.main:app`."""

from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from atlas import __version__, analytics, auth, observability, ratelimit
from atlas.auth import Principal
from atlas.config import settings
from atlas.data import access
from atlas.data.db import Alert, AlertEvent, init_db, session_scope
from atlas.instruments import datasets, get_dataset, resolve, sync_registry
from atlas.research import engine, llm
from atlas.research import tools as T

log = logging.getLogger("atlas.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with session_scope() as s:
        sync_registry(s)
    if os.getenv("ATLAS_SCHEDULER", "off").lower() == "on":
        from atlas.ingestion.scheduler import run_forever

        threading.Thread(target=run_forever, daemon=True, name="ingestion-scheduler").start()
    yield


API_DESCRIPTION = """
Evidence-grounded market, macro and trade research.

**Authentication.** Send `Authorization: Bearer atk_…` with an API key created on the Account page
(click **Authorize** above to try requests here). Keys act on one workspace; research runs, reports,
alerts, schedules and portfolios are private to it. Market and macro data are shared.

**Research jobs** are asynchronous: `POST /api/v1/research` returns `202` with a `run_id`; poll
`GET /api/v1/research/{run_id}` until `status` is `complete`, then download the report.

**Limits.** Plan quotas apply (HTTP 429 when exceeded). Data are public reference/benchmark series
published with a lag, not live tradable quotes. Research and decision support only, not investment advice.
"""

app = FastAPI(title="ATLAS API", version=__version__, lifespan=lifespan, description=API_DESCRIPTION)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"],
                   allow_credentials=True)
V1 = "/api/v1"
SESSION_COOKIE = "atlas_session"


_bearer = HTTPBearer(auto_error=False, scheme_name="ApiKey",
                     description="An API key (`atk_…`) from the Account page, or a session token from /auth/login.")


def principal(request: Request, response: Response, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    """Authenticate via session cookie or `Authorization: Bearer <session token | API key>`."""
    if not auth.auth_required():
        return auth.LOCAL_PRINCIPAL
    token = creds.credentials.strip() if creds else request.cookies.get(SESSION_COOKIE)
    try:
        p = auth.authenticate(token)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
    except auth.WorkspaceSuspended as exc:
        raise HTTPException(403, {"error": "workspace_suspended", "message": str(exc)}) from exc
    if p.needs_2fa and not (p.via == "session" and request.url.path in TWO_FACTOR_SETUP_PATHS):
        raise HTTPException(403, {"error": "two_factor_required",
                                  "message": "This workspace requires two-factor authentication. Turn it on from your Account page."})
    limit = int(auth.PLANS.get(p.plan, auth.PLANS["explorer"]).get("requests_per_minute") or 0)
    if limit and p.via == "session":
        limit = max(300, 5 * limit)  # the web app makes several calls per page and polls running research
    if limit:
        allowed, remaining, retry = ratelimit.take(f"{p.workspace_id}|{p.user_id}|{p.via}", limit)
        headers = {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining)}
        if not allowed:
            raise HTTPException(429, f"rate limit of {limit} requests per minute reached for the {p.plan.replace('_', ' ')} plan; "
                                     f"retry in {retry}s", headers={**headers, "Retry-After": str(retry)})
        response.headers.update(headers)
    return p


# What a member of a 2FA-required workspace can reach before enrolling (browser sessions only).
TWO_FACTOR_SETUP_PATHS = {f"{V1}/account", f"{V1}/account/2fa/setup", f"{V1}/account/2fa/enable", f"{V1}/auth/switch-workspace"}


def require_admin(p: Principal = Depends(principal)) -> Principal:
    if p.role not in ("owner", "admin") or p.workspace_id != auth.LOCAL_WORKSPACE and auth.auth_required():
        raise HTTPException(403, "operator access required")
    return p


def require_verified(p: Principal = Depends(principal)) -> Principal:
    try:
        auth.require_verified(p)
    except auth.VerificationRequired as exc:
        raise HTTPException(403, str(exc)) from exc
    return p


def _quota(workspace_id: str, resource: str) -> None:
    try:
        auth.check_quota(workspace_id, resource)
    except auth.QuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc


api = APIRouter(dependencies=[Depends(principal)])


_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    """Every response carries X-Request-ID (the caller's, if well formed); logs and error bodies include it."""
    incoming = request.headers.get("x-request-id", "")
    rid = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
    request.state.request_id = rid
    token = observability.request_id.set(rid)
    try:
        response = await call_next(request)
    finally:
        observability.request_id.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse

    rid = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    log.error("unhandled error on %s %s [request %s]", request.method, request.url.path, rid, exc_info=exc)
    return JSONResponse({"detail": "internal error; quote this request id if you contact support", "request_id": rid},
                        status_code=500, headers={"X-Request-ID": rid})


@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    import time as _time

    from atlas import observability

    start = _time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    template = getattr(route, "path", None) or "unmatched"  # route template keeps label cardinality bounded
    if template != "unmatched" and request.url.path.startswith(V1) and not template.startswith(V1):
        template = V1 + template  # routes on the included router report their template without the prefix
    observability.record(request.method, template, response.status_code, _time.perf_counter() - start)
    return response


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    """Prometheus scrape endpoint. Set ATLAS_METRICS_TOKEN to require `Authorization: Bearer <token>`."""
    import hmac

    from fastapi.responses import PlainTextResponse

    from atlas import observability

    expected = os.getenv("ATLAS_METRICS_TOKEN")
    if expected:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, expected):
            raise HTTPException(401, "metrics token required")
    return PlainTextResponse(observability.render(), media_type="text/plain; version=0.0.4")


def _404(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


def _unavailable(exc: Exception):
    raise HTTPException(status_code=409, detail={"error": "data_unavailable", "message": str(exc)})


class SignupRequest(BaseModel):
    email: str = Field(..., max_length=320)
    password: str = Field(..., max_length=500)
    name: str = Field("", max_length=200)
    workspace_name: str | None = Field(None, max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=320)
    password: str = Field(..., max_length=500)


def _set_session(response: Response, token: str) -> None:
    secure = os.getenv("ATLAS_COOKIE_SECURE", "false").lower() == "true"
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=secure,
                        max_age=int(auth.SESSION_TTL.total_seconds()), path="/")


@app.post(f"{V1}/auth/signup", status_code=201)
def signup(req: SignupRequest, request: Request, response: Response) -> dict[str, Any]:
    if not auth.signup_allowed():
        raise HTTPException(403, "self-service signup is disabled")
    try:
        uid, _ = auth.create_user(req.email, req.password, req.name, req.workspace_name)
        auth.send_verification(uid)
        token, p = auth.login(req.email, req.password, request.client.host if request.client else "")
    except auth.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    _set_session(response, token)
    return {"token": token, **auth.account(p)}


@app.post(f"{V1}/auth/login")
def login(req: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    """Password sign-in. With two-factor authentication on, returns `two_factor_required` and a short-lived
    `challenge` instead of a session; finish at /auth/login/2fa."""
    try:
        token, p = auth.login(req.email, req.password, request.client.host if request.client else "")
    except auth.SecondFactorRequired as need:
        return {"two_factor_required": True, "challenge": need.challenge}
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    _set_session(response, token)
    return {"token": token, **auth.account(p)}


class SecondFactorRequest(BaseModel):
    challenge: str = Field(..., max_length=200)
    code: str = Field(..., max_length=20)


@app.post(f"{V1}/auth/login/2fa")
def login_2fa(req: SecondFactorRequest, request: Request, response: Response) -> dict[str, Any]:
    try:
        token, p = auth.login_second_factor(req.challenge, req.code, request.client.host if request.client else "")
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    _set_session(response, token)
    return {"token": token, **auth.account(p)}


class ResetRequest(BaseModel):
    email: str = Field(..., max_length=320)


class ResetConfirm(BaseModel):
    token: str = Field(..., max_length=200)
    password: str = Field(..., max_length=500)


@app.post(f"{V1}/auth/password-reset/request", status_code=202)
def password_reset_request(req: ResetRequest, request: Request) -> dict[str, Any]:
    try:
        auth.request_password_reset(req.email, request.client.host if request.client else "")
    except auth.AuthError as exc:  # throttled
        raise HTTPException(429, str(exc)) from exc
    # Same response whether or not the account exists.
    return {"status": "accepted", "message": "If an account exists for that email, a reset link has been sent."}


@app.post(f"{V1}/auth/password-reset/confirm", status_code=204)
def password_reset_confirm(req: ResetConfirm) -> None:
    try:
        auth.reset_password(req.token, req.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    except auth.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


class VerifyRequest(BaseModel):
    token: str = Field(..., max_length=200)


@app.post(f"{V1}/auth/verify-email", status_code=204)
def verify_email(req: VerifyRequest) -> None:
    try:
        auth.verify_email(req.token)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc


class InvitationAccept(BaseModel):
    token: str = Field(..., max_length=200)
    password: str | None = Field(None, max_length=500)  # only for a new account
    name: str = Field("", max_length=200)


def _invite_throttle(request: Request) -> None:
    try:
        auth._throttle(f"invite|{request.client.host if request.client else ''}")
    except auth.AuthError as exc:
        raise HTTPException(429, "too many attempts; try again later") from exc


@app.get(f"{V1}/auth/invitations/{{token}}")
def invitation_details(token: str, request: Request) -> dict[str, Any]:
    from atlas import teams

    try:
        return teams.describe(token)
    except auth.AuthError as exc:
        _invite_throttle(request)
        raise HTTPException(404, str(exc)) from exc


@app.post(f"{V1}/auth/invitations/accept")
def accept_invitation(req: InvitationAccept, request: Request, response: Response) -> dict[str, Any]:
    """Join a workspace: signed in as the invited email, or by creating that account."""
    from atlas import teams

    session = request.cookies.get(SESSION_COOKIE)
    try:
        if session:
            p = auth.authenticate(session)
            ws_id = teams.accept(req.token, p.user_id)
            teams.switch_workspace(session, p.user_id, ws_id)
            return auth.account(auth.authenticate(session))
        info = teams.describe(req.token)
        if info["has_account"]:
            raise HTTPException(401, f"sign in as {info['email']} to accept this invitation")
        if not req.password:
            raise HTTPException(422, "choose a password to create your account")
        uid, _ = teams.signup_with_invitation(req.token, req.password, req.name)
        auth.send_verification(uid)
        token, p = auth.login(info["email"], req.password, request.client.host if request.client else "")
    except auth.AuthError as exc:
        _invite_throttle(request)
        raise HTTPException(400, str(exc)) from exc
    except auth.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    _set_session(response, token)
    return {"token": token, **auth.account(p)}


@app.post(f"{V1}/auth/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth.logout(token)
    response.delete_cookie(SESSION_COOKIE, path="/")


@app.get("/health/ready")
def readiness() -> Response:
    """Readiness for load balancers and orchestrators: 200 when the database answers (503 otherwise).
    Also reports whether a scheduler is active and when it last ran (informational)."""
    import json

    from sqlalchemy import text

    from atlas.data.db import get_engine
    from atlas.ingestion import scheduler

    checks: dict[str, Any] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
        checks["scheduler"] = scheduler.status()
    except Exception as exc:  # noqa: BLE001 - report, do not crash the probe
        checks["database"] = f"error: {type(exc).__name__}"
    ready = checks["database"] == "ok"
    return Response(json.dumps({"ready": ready, "version": __version__, **checks}), status_code=200 if ready else 503,
                    media_type="application/json")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "llm_configured": llm.available(), "llm_model": settings.llm_model,
            "auth_required": auth.auth_required(), "signup_allowed": auth.signup_allowed(),
            "commercial_mode": settings.commercial_mode}


# ---------------------------------------------------------------- account


@api.get("/account")
def get_account(p: Principal = Depends(principal)) -> dict[str, Any]:
    return auth.account(p)


@api.post("/account/verify-email/resend", status_code=202)
def resend_verification(request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    try:
        auth._throttle(f"verify|{p.user_id}|{request.client.host if request.client else ''}")
    except auth.AuthError as exc:
        raise HTTPException(429, str(exc)) from exc
    sent = auth.send_verification(p.user_id)
    return {"status": "sent" if sent else "already_verified"}


class PasswordChange(BaseModel):
    current_password: str = Field(..., max_length=500)
    new_password: str = Field(..., max_length=500)


@api.post("/account/password", status_code=204)
def change_password(req: PasswordChange, response: Response, p: Principal = Depends(principal)) -> None:
    if p.via != "session":
        raise HTTPException(403, "sign in to change your password")
    try:
        auth.change_password(p, req.current_password, req.new_password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    except auth.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    response.delete_cookie(SESSION_COOKIE, path="/")


class ApiKeyRequest(BaseModel):
    name: str = Field("API key", max_length=200)


@api.get("/api-keys")
def list_api_keys(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    return auth.list_api_keys(p)


@api.post("/api-keys", status_code=201)
def create_api_key(req: ApiKeyRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    if p.via == "api_key":
        raise HTTPException(403, "API keys cannot create API keys; sign in")
    _quota(p.workspace_id, "api_keys")
    return auth.create_api_key(p, req.name)


@api.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(key_id: str, p: Principal = Depends(principal)) -> None:
    if not auth.revoke_api_key(p, key_id):
        _404("API key")


# ---------------------------------------------------------------- webhooks


class WebhookCreate(BaseModel):
    url: str = Field(..., max_length=2000)
    events: list[str] = Field(..., min_length=1, max_length=10)
    description: str = Field("", max_length=200)
    format: Literal["atlas", "slack"] = "atlas"


class WebhookUpdate(BaseModel):
    events: list[str] | None = Field(None, min_length=1, max_length=10)
    description: str | None = Field(None, max_length=200)
    status: Literal["active", "paused"] | None = None


def require_manager(p: Principal = Depends(principal)) -> Principal:
    if p.role not in ("owner", "admin"):
        raise HTTPException(403, "only workspace owners and admins can manage webhooks")
    return p


def _webhook_call(fn, *args, **kwargs):
    from atlas import webhooks

    try:
        return fn(*args, **kwargs)
    except KeyError:
        _404("webhook")
    except webhooks.WebhookError as exc:
        raise HTTPException(422, str(exc)) from exc


@api.get("/webhooks")
def list_webhooks(p: Principal = Depends(require_manager)) -> dict[str, Any]:
    from atlas import webhooks

    return {"webhooks": webhooks.list_webhooks(p.workspace_id), "events": webhooks.EVENTS}


@api.post("/webhooks", status_code=201)
def create_webhook(req: WebhookCreate, p: Principal = Depends(require_manager)) -> dict[str, Any]:
    from atlas import webhooks

    if p.via == "api_key":
        raise HTTPException(403, "API keys cannot create webhooks; sign in")
    _quota(p.workspace_id, "webhooks")
    return _webhook_call(webhooks.create, p, req.url, req.events, req.description, req.format)


@api.patch("/webhooks/{webhook_id}")
def update_webhook(webhook_id: str, req: WebhookUpdate, p: Principal = Depends(require_manager)) -> dict[str, Any]:
    from atlas import webhooks

    return _webhook_call(webhooks.update_webhook, p, webhook_id, events=req.events, description=req.description, status=req.status)


@api.delete("/webhooks/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: str, p: Principal = Depends(require_manager)) -> None:
    from atlas import webhooks

    _webhook_call(webhooks.delete_webhook, p, webhook_id)


@api.post("/webhooks/{webhook_id}/rotate-secret")
def rotate_webhook_secret(webhook_id: str, p: Principal = Depends(require_manager)) -> dict[str, Any]:
    from atlas import webhooks

    if p.via == "api_key":
        raise HTTPException(403, "API keys cannot rotate webhook secrets; sign in")
    return _webhook_call(webhooks.rotate_secret, p, webhook_id)


@api.post("/webhooks/{webhook_id}/test")
def test_webhook(webhook_id: str, p: Principal = Depends(require_manager)) -> dict[str, Any]:
    from atlas import webhooks

    return _webhook_call(webhooks.send_test, p, webhook_id)


@api.get("/webhooks/{webhook_id}/deliveries")
def webhook_deliveries(webhook_id: str, limit: int = Query(50, ge=1, le=200), p: Principal = Depends(require_manager)) -> list[dict[str, Any]]:
    from atlas import webhooks

    return _webhook_call(webhooks.deliveries, p.workspace_id, webhook_id, limit)


# ---------------------------------------------------------------- newsletter


class NewsletterSettings(BaseModel):
    slug: str = Field(..., max_length=40)
    name: str = Field(..., max_length=120)
    description: str = Field("", max_length=2000)
    from_name: str = Field("", max_length=120)
    reply_to: str | None = Field(None, max_length=320)
    postal_address: str = Field("", max_length=300)
    public_archive: bool = True
    language: Literal["en", "es", "pt"] = "en"


class SubscribeRequest(BaseModel):
    email: str = Field(..., max_length=320)


class IssueCreate(BaseModel):
    run_id: str = Field(..., max_length=32)
    subject: str | None = Field(None, max_length=200)
    intro: str = Field("", max_length=5000)


class IssueUpdate(BaseModel):
    subject: str | None = Field(None, max_length=200)
    intro: str | None = Field(None, max_length=5000)


def require_publisher(p: Principal = Depends(principal)) -> Principal:
    if p.role not in ("owner", "admin"):
        raise HTTPException(403, "only workspace owners and admins can manage the newsletter")
    return p


def _newsletter_call(fn, *args, **kwargs):
    from atlas import newsletters

    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        _404("newsletter" if exc.args and exc.args[0] == "newsletter" else "issue or subscriber")
    except newsletters.NewsletterError as exc:
        raise HTTPException(422, str(exc)) from exc


@api.get("/newsletter")
def newsletter_settings(p: Principal = Depends(require_publisher)) -> dict[str, Any]:
    from atlas import mailer, newsletters

    return {"newsletter": newsletters.get_settings(p.workspace_id), "email_configured": mailer.configured()}


@api.put("/newsletter")
def save_newsletter(req: NewsletterSettings, p: Principal = Depends(require_publisher)) -> dict[str, Any]:
    from atlas import newsletters

    return _newsletter_call(newsletters.save_settings, p, **req.model_dump())


@api.get("/newsletter/subscribers")
def newsletter_subscribers(format: Literal["json", "csv"] = "json", p: Principal = Depends(require_publisher)):
    from atlas import newsletters

    if format == "csv":
        return Response(_newsletter_call(newsletters.subscribers_csv, p.workspace_id), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="atlas-subscribers-{date.today().isoformat()}.csv"'})
    return _newsletter_call(newsletters.list_subscribers, p.workspace_id)


@api.post("/newsletter/subscribers", status_code=202)
def invite_subscriber(req: SubscribeRequest, p: Principal = Depends(require_publisher)) -> dict[str, Any]:
    from atlas import newsletters

    _newsletter_call(newsletters.invite, p, req.email)
    return {"status": "confirmation_sent"}


@api.delete("/newsletter/subscribers/{subscriber_id}", status_code=204)
def remove_subscriber(subscriber_id: str, p: Principal = Depends(require_publisher)) -> None:
    from atlas import newsletters

    _newsletter_call(newsletters.remove_subscriber, p, subscriber_id)


@api.get("/newsletter/issues")
def newsletter_issues(p: Principal = Depends(require_publisher)) -> list[dict[str, Any]]:
    from atlas import newsletters

    return _newsletter_call(newsletters.list_issues, p.workspace_id)


@api.post("/newsletter/issues", status_code=201)
def create_issue(req: IssueCreate, p: Principal = Depends(require_publisher)) -> dict[str, Any]:
    from atlas import newsletters

    return _newsletter_call(newsletters.create_issue, p, req.run_id, req.subject, req.intro)


@api.get("/newsletter/issues/{issue_id}")
def get_issue(issue_id: str, p: Principal = Depends(require_publisher)) -> dict[str, Any]:
    from atlas import newsletters

    return _newsletter_call(newsletters.get_issue, p.workspace_id, issue_id)


@api.patch("/newsletter/issues/{issue_id}")
def update_issue(issue_id: str, req: IssueUpdate, p: Principal = Depends(require_publisher)) -> dict[str, Any]:
    from atlas import newsletters

    return _newsletter_call(newsletters.update_issue, p, issue_id, subject=req.subject, intro=req.intro)


@api.delete("/newsletter/issues/{issue_id}", status_code=204)
def delete_issue(issue_id: str, p: Principal = Depends(require_publisher)) -> None:
    from atlas import newsletters

    _newsletter_call(newsletters.delete_issue, p, issue_id)


@api.post("/newsletter/issues/{issue_id}/test")
def test_issue(issue_id: str, p: Principal = Depends(require_publisher)) -> dict[str, Any]:
    from atlas import newsletters

    return _newsletter_call(newsletters.send_test, p, issue_id)


@api.post("/newsletter/issues/{issue_id}/send")
def send_issue(issue_id: str, p: Principal = Depends(require_publisher)) -> dict[str, Any]:
    from atlas import newsletters

    if p.via != "session":
        raise HTTPException(403, "send issues from a signed-in browser session")
    return _newsletter_call(newsletters.send_issue, p, issue_id)


# ---------------------------------------------------------------- onboarding


EXAMPLE_PORTFOLIO = {
    "name": "Example: commodity tilt",
    "holdings": [{"dataset_id": "CMD.CRUDE.BRENT", "weight": 0.3}, {"dataset_id": "CMD.COPPER.LME", "weight": 0.3},
                 {"dataset_id": "EQ.US.SP500", "weight": 0.4}],
}


@api.get("/onboarding")
def onboarding(p: Principal = Depends(principal)) -> dict[str, Any]:
    """First-run checklist, computed from what the workspace has actually done."""
    from atlas import agents, portfolios

    runs = engine.list_runs(1, p.workspace_id)
    steps = [
        {"key": "research", "title": "Ask your first research question", "done": bool(runs), "href": "/research",
         "help": "Pick an example or ask your own; every figure is cited and verified."},
        {"key": "portfolio", "title": "Analyze a portfolio", "done": bool(portfolios.list_portfolios(p.workspace_id)), "href": "/portfolio",
         "help": "See volatility, value at risk and stress tests for your holdings."},
        {"key": "agent", "title": "Set up a monitoring agent", "done": bool(agents.list_agents(p.workspace_id)), "href": "/intelligence",
         "help": "Get a research report automatically when a market does something unusual."},
        {"key": "api_key", "title": "Create an API key", "done": bool(auth.list_api_keys(p)) or p.via == "local", "href": "/account",
         "help": "Use ATLAS from scripts and notebooks."},
    ]
    return {"steps": steps, "complete": all(s["done"] for s in steps)}


@api.post("/onboarding/example-portfolio", status_code=201)
def create_example_portfolio(p: Principal = Depends(require_verified)) -> dict[str, Any]:
    """One click to a working Portfolio page: a clearly labelled example the user can edit or delete."""
    from atlas import portfolios

    existing = portfolios.find_by_name(EXAMPLE_PORTFOLIO["name"], p.workspace_id)
    if existing:
        return existing
    return portfolios.save(EXAMPLE_PORTFOLIO["name"], EXAMPLE_PORTFOLIO["holdings"], p.workspace_id, p.user_id)


# ---------------------------------------------------------------- reference & market data


@api.get(f"/instruments")
def list_instruments() -> list[dict[str, Any]]:
    status = {r["dataset_id"]: r for r in access.data_status()}
    out = []
    for d in datasets().values():
        st = status[d.dataset_id]
        out.append({
            "dataset_id": d.dataset_id, "name": d.name, "kind": d.kind,
            "asset_class": d.meta.get("asset_class", "macro"), "frequency": d.frequency, "unit": d.unit,
            "price_basis": d.meta.get("price_basis", "economic_indicator"), "description": d.meta.get("description", ""),
            "has_source": d.has_source, "observation_date": st["observation_date"], "freshness": st["freshness"],
            "coverage_note": d.meta.get("coverage_note"), "licence": st.get("licence"),
        })
    return out


@api.get(f"/instruments/search")
def search_instruments(q: str = Query(..., min_length=1), limit: int = 8) -> list[dict[str, Any]]:
    return resolve(q, limit=limit)


@api.get(f"/markets/quotes")
def quotes(ids: str = Query(..., description="comma-separated dataset ids")) -> list[dict[str, Any]]:
    out = []
    for dsid in [i.strip() for i in ids.split(",") if i.strip()]:
        if not get_dataset(dsid):
            out.append({"dataset_id": dsid, "error": "unknown dataset"})
            continue
        try:
            out.append(access.latest_quote(dsid))
        except access.DataUnavailable as exc:
            m = access.series_meta(dsid)
            out.append({"dataset_id": dsid, "name": m.name, "error": "data_unavailable", "freshness": m.freshness, "freshness_note": m.freshness_note})
    return out


def _history(dsid: str, start: date | None, end: date | None, lookback_years: float | None, as_of=None) -> dict[str, Any]:
    ds = get_dataset(dsid) or _404("dataset")
    if lookback_years and not start:
        start = date.today() - timedelta(days=int(lookback_years * 365.25))
    try:
        s, meta = access.get_series(dsid, start=start, end=end, as_of=as_of)
    except access.DataUnavailable as exc:
        _unavailable(exc)
    return {
        "meta": meta.to_dict(),
        "points": [{"date": d.date().isoformat(), "value": float(v)} for d, v in s.items()],
        "performance": analytics.performance(s, unit=ds.unit, frequency=ds.frequency) if len(s) > 2 else None,  # type: ignore[union-attr]
    }


@api.get(f"/markets/history")
def market_history(id: str, start: date | None = None, end: date | None = None, lookback_years: float | None = 5) -> dict[str, Any]:
    return _history(id, start, end, lookback_years)


@api.get(f"/macro/series")
def macro_series(id: str, start: date | None = None, end: date | None = None, lookback_years: float | None = 10,
                 as_of: str | None = Query(None, description="ISO datetime for a point-in-time (vintage) view")) -> dict[str, Any]:
    from datetime import datetime

    return _history(id, start, end, lookback_years, datetime.fromisoformat(as_of) if as_of else None)


@api.get(f"/futures/curve")
def futures_curve(underlying: str) -> JSONResponse:
    return JSONResponse(status_code=501, content={
        "error": "not_connected",
        "message": "Futures curves require a licensed ICE/CME settlement feed, which is not connected in this deployment.",
        "underlying": underlying,
    })


@api.get(f"/data/status")
def data_status() -> list[dict[str, Any]]:
    return access.data_status()


@api.get("/calendar")
def economic_calendar(days: int = Query(60, ge=1, le=400)) -> dict[str, Any]:
    from atlas import calendar

    return calendar.upcoming(days)


class WatchlistRequest(BaseModel):
    dataset_ids: list[str] = Field(..., max_length=40)


def _watch(fn, *args) -> dict[str, Any]:
    from atlas import watchlist

    try:
        return {"dataset_ids": fn(*args)}
    except watchlist.WatchlistError as exc:
        raise HTTPException(422, str(exc)) from exc


@api.get("/watchlist")
def get_watchlist(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import watchlist

    return {"dataset_ids": watchlist.items(p.workspace_id)}


@api.put("/watchlist")
def set_watchlist(req: WatchlistRequest, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import watchlist

    return _watch(watchlist.replace, p.workspace_id, req.dataset_ids, p.user_id)


@api.post("/watchlist/{dataset_id}")
def watch(dataset_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import watchlist

    return _watch(watchlist.add, p.workspace_id, dataset_id, p.user_id)


@api.delete("/watchlist/{dataset_id}")
def unwatch(dataset_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import watchlist

    return _watch(watchlist.remove, p.workspace_id, dataset_id)


@api.get("/briefing")
def briefing(days: int = Query(14, ge=1, le=90)) -> dict[str, Any]:
    from atlas import briefing as b

    return b.week(days)


@api.get("/documents")
def search_documents(q: str = "", days: int = Query(30, ge=1, le=730), limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    from atlas import documents

    return documents.search(q, days=days, limit=limit)


@api.get("/datasets/{dataset_id}/export")
def export_dataset(dataset_id: str, format: Literal["csv", "json"] = "csv", start: date | None = None, end: date | None = None,
                   as_of: datetime | None = None, header: bool = True, p: Principal = Depends(principal)):
    """Download a stored dataset with provenance. `as_of` gives the point-in-time view for macro series."""
    from atlas import exports

    if get_dataset(dataset_id) is None:
        _404("dataset")
    try:
        body, media = exports.export(dataset_id, format, start=start, end=end, as_of=as_of, header=header)
    except access.DataUnavailable as exc:
        _unavailable(exc)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    auth.audit("data.exported", p.user_id, p.workspace_id, dataset_id=dataset_id, format=format)
    stamp = (as_of.date() if as_of else date.today()).isoformat()
    return Response(body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{dataset_id.lower()}-{stamp}.{format}"', "X-Content-Type-Options": "nosniff"})


@api.get(f"/pipelines/status")
def pipelines_status(limit: int = 50) -> dict[str, Any]:
    return access.pipeline_status(limit)


class IngestRequest(BaseModel):
    dataset_ids: list[str] | None = None
    backfill: bool = False


@api.post(f"/pipelines/run", status_code=202)
def pipelines_run(req: IngestRequest, p: Principal = Depends(require_admin)) -> dict[str, Any]:
    from atlas.ingestion.pipeline import ingest

    threading.Thread(target=ingest, args=(req.dataset_ids,), kwargs={"backfill": req.backfill}, daemon=True).start()
    return {"status": "accepted", "dataset_ids": req.dataset_ids or "all"}


# ---------------------------------------------------------------- personal data


class DeleteAccountRequest(BaseModel):
    password: str = Field(..., max_length=500)
    code: str | None = Field(None, max_length=20)
    confirm_email: str = Field(..., max_length=320)


class TransferRequest(BaseModel):
    user_id: str = Field(..., max_length=64)


@api.get("/account/export")
def export_my_data(p: Principal = Depends(principal)):
    from atlas import privacy

    if p.via == "local":
        raise HTTPException(400, "single-user mode has no personal account")
    import json

    body = json.dumps(privacy.export_personal_data(p), indent=2, default=str)
    return Response(body, media_type="application/json", headers={
        "Content-Disposition": f'attachment; filename="atlas-my-data-{date.today().isoformat()}.json"'})


@api.get("/account/deletion-check")
def deletion_check(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import privacy

    problems, purge = privacy.deletion_blockers(p.user_id)
    return {"can_delete": not problems, "blockers": problems, "workspaces_deleted": purge}


@api.post("/account/delete")
def delete_my_account(req: DeleteAccountRequest, request: Request, response: Response, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import privacy

    if p.via != "session":
        raise HTTPException(400, "delete your account from a signed-in browser session")
    if req.confirm_email.strip().lower() != p.email:
        raise HTTPException(422, "type your account email to confirm")
    try:
        auth._throttle(f"delete|{p.user_id}|{request.client.host if request.client else ''}")
        out = privacy.delete_account(p, req.password, req.code)
    except auth.AuthError as exc:
        raise HTTPException(403, str(exc)) from exc
    except auth.ValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    response.delete_cookie(SESSION_COOKIE, path="/")
    return out


@api.post("/team/transfer-ownership", status_code=204)
def transfer_ownership(req: TransferRequest, p: Principal = Depends(principal)) -> None:
    from atlas import privacy

    try:
        privacy.transfer_ownership(p, req.user_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError:
        _404("member")


# ---------------------------------------------------------------- operator console


def require_operator(p: Principal = Depends(principal)) -> Principal:
    """Operators (ATLAS_OPERATOR_EMAILS) in a browser session with two-factor authentication on."""
    from atlas.data.db import User

    if p.via != "session" or not auth.is_operator(p.email):
        raise HTTPException(403, "operator access required")
    with session_scope() as s:
        user = s.get(User, p.user_id)
        if not (user and user.totp_enabled_at):
            raise HTTPException(403, "operators must turn on two-factor authentication first (Account page)")
    return p


class AdminWorkspaceUpdate(BaseModel):
    plan: str | None = Field(None, max_length=32)
    suspended: bool | None = None
    reason: str = Field("", max_length=200)


@api.get("/admin/overview")
def admin_overview(p: Principal = Depends(require_operator)) -> dict[str, Any]:
    from atlas import admin

    return admin.overview()


@api.get("/admin/workspaces")
def admin_workspaces(p: Principal = Depends(require_operator)) -> list[dict[str, Any]]:
    from atlas import admin

    return admin.workspaces()


@api.patch("/admin/workspaces/{workspace_id}")
def admin_update_workspace(workspace_id: str, req: AdminWorkspaceUpdate, p: Principal = Depends(require_operator)) -> dict[str, Any]:
    from atlas import admin

    try:
        if req.plan is not None:
            admin.change_plan(p, workspace_id, req.plan)
        if req.suspended is not None:
            admin.set_suspended(p, workspace_id, req.suspended, req.reason)
    except auth.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except KeyError:
        _404("workspace")
    return next(w for w in admin.workspaces() if w["workspace_id"] == workspace_id)


class FeedbackResolve(BaseModel):
    status: Literal["open", "resolved"]
    note: str | None = Field(None, max_length=2000)


@api.get("/admin/doctor")
def admin_doctor(p: Principal = Depends(require_operator)) -> dict[str, Any]:
    """The deployment's configuration check (`atlas doctor`), for the operator console."""
    from atlas import doctor

    return doctor.run()


@api.get("/admin/feedback")
def admin_feedback(status: Literal["open", "resolved", "all"] = "open", p: Principal = Depends(require_operator)) -> dict[str, Any]:
    from atlas import feedback

    return {"feedback": feedback.list_for_operators(None if status == "all" else status), "summary": feedback.summary()}


@api.patch("/admin/feedback/{feedback_id}")
def admin_resolve_feedback(feedback_id: str, req: FeedbackResolve, p: Principal = Depends(require_operator)) -> dict[str, Any]:
    from atlas import feedback

    try:
        return feedback.resolve(p, feedback_id, req.status, req.note)
    except KeyError:
        _404("feedback")


class FeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    reasons: list[str] = Field(default_factory=list, max_length=6)
    comment: str = Field("", max_length=2000)


@api.get("/research/{run_id}/feedback")
def my_feedback(run_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import feedback

    return {"feedback": feedback.get_mine(p, run_id), "reasons": feedback.REASONS}


@api.put("/research/{run_id}/feedback")
def give_feedback(run_id: str, req: FeedbackRequest, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import feedback

    if p.via == "local":
        raise HTTPException(400, "feedback needs an account (single-user mode has no operators to send it to)")
    try:
        return feedback.submit(p, run_id, req.rating, req.reasons, req.comment)
    except KeyError:
        _404("research run")
    except feedback.FeedbackError as exc:
        raise HTTPException(422, str(exc)) from exc


@api.delete("/research/{run_id}/feedback", status_code=204)
def withdraw_feedback(run_id: str, p: Principal = Depends(principal)) -> None:
    from atlas import feedback

    if not feedback.withdraw(p, run_id):
        _404("feedback")


# ---------------------------------------------------------------- weekly digest


class DigestPreference(BaseModel):
    weekly: bool


@api.get("/account/digest")
def get_digest(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import digest

    return {"weekly": digest.opted_in(p.user_id, p.workspace_id), "send_time": "Mondays from 07:00 UTC"}


@api.put("/account/digest")
def set_digest(req: DigestPreference, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import digest

    if p.via == "local":
        raise HTTPException(400, "single-user mode has no email address")
    digest.set_opt_in(p.user_id, p.workspace_id, req.weekly)
    auth.audit("digest.subscribed" if req.weekly else "digest.unsubscribed", p.user_id, p.workspace_id)
    return {"weekly": req.weekly}


@api.get("/account/digest/preview")
def preview_digest(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import digest

    if p.via == "local":
        raise HTTPException(400, "single-user mode has no email address")
    msg = digest.compose(p.user_id, p.workspace_id)
    return {"subject": msg["subject"], "body": msg["body"]}


@app.get("/digest/unsubscribe", include_in_schema=False)
def digest_unsubscribe(u: str, w: str, s: str) -> Response:
    """One-click unsubscribe from an email link (signed; no sign-in needed)."""
    import html as _html

    from atlas import digest

    ok = digest.unsubscribe(u[:64], w[:64], s[:64])
    if ok:
        auth.audit("digest.unsubscribed", u[:64], w[:64], via="email link")
    msg = "You will no longer receive the ATLAS weekly email for this workspace." if ok else "This unsubscribe link is not valid."
    page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>ATLAS</title>"
            f"<body style='font-family:system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
            f"<h1 style='font-size:1.2rem'>ATLAS</h1><p>{_html.escape(msg)}</p></body>")
    return Response(page, status_code=200 if ok else 400, media_type="text/html",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'", "X-Robots-Tag": "noindex"})


def _plain_page(msg: str, status: int = 200, lang: str = "en") -> Response:
    import html as _html

    page = (f"<!doctype html><html lang={lang}><meta charset=utf-8><meta name=viewport content='width=device-width'><title>ATLAS</title>"
            f"<body style='font-family:system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
            f"<h1 style='font-size:1.2rem'>ATLAS</h1><p>{_html.escape(msg)}</p></body>")
    return Response(page, status_code=status, media_type="text/html",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'", "X-Robots-Tag": "noindex"})


def _page_lang(request: Request) -> str:
    """For pages that cannot tell which newsletter they belong to: the reader's preferred language."""
    accept = request.headers.get("accept-language", "").lower()
    first = accept.split(",")[0].split("-")[0].strip()
    return first if first in ("es", "pt") else "en"


def _client_throttle(request: Request, what: str) -> None:
    try:
        auth._throttle(f"{what}|{request.client.host if request.client else ''}")
    except auth.AuthError as exc:
        raise HTTPException(429, "too many requests; try again later") from exc


@app.get(f"{V1}/public/plans", include_in_schema=False)
def public_plans() -> dict[str, Any]:
    """Plans and their limits for the public pricing page (no sign-in)."""
    from atlas import billing

    return billing.public_plans()


@app.get(f"{V1}/public/newsletters/{{slug}}", include_in_schema=False)
def public_newsletter(slug: str) -> dict[str, Any]:
    from atlas import newsletters

    info = newsletters.public_info(slug[:40])
    if info is None:
        _404("newsletter")
    return info


@app.get(f"{V1}/public/newsletters/{{slug}}/issues/{{issue_id}}", include_in_schema=False)
def public_newsletter_issue(slug: str, issue_id: str) -> dict[str, Any]:
    from atlas import newsletters

    issue = newsletters.public_issue(slug[:40], issue_id[:32])
    if issue is None:
        _404("issue")
    return issue


@app.post(f"{V1}/public/newsletters/{{slug}}/subscribe", status_code=202, include_in_schema=False)
def public_subscribe(slug: str, req: SubscribeRequest, request: Request) -> dict[str, Any]:
    """Double opt-in: always answers the same way, so it reveals nothing about who is subscribed."""
    from atlas import newsletters

    _client_throttle(request, "subscribe")
    try:
        newsletters.subscribe(slug[:40], req.email)
    except KeyError:
        _404("newsletter")
    except newsletters.NewsletterError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"status": "check_your_email"}


@app.get("/newsletter/confirm", include_in_schema=False)
def newsletter_confirm(token: str, request: Request) -> Response:
    from atlas import newsletters

    from atlas import i18n

    done = newsletters.confirm(token[:100])
    if done is None:
        _client_throttle(request, "newsletter-confirm")
        return _plain_page(i18n.newsletter_text(_page_lang(request), "confirm_invalid"), 400, _page_lang(request))
    return _plain_page(i18n.newsletter_text(done["language"], "confirmed", name=done["name"]), lang=i18n.lang(done["language"]))


@app.api_route("/newsletter/unsubscribe", methods=["GET", "POST"], include_in_schema=False)
def newsletter_unsubscribe(s: str, sig: str, request: Request) -> Response:
    """One-click unsubscribe (GET from the link, POST from mail clients' List-Unsubscribe-Post)."""
    from atlas import newsletters

    from atlas import i18n

    done = newsletters.unsubscribe(s[:32], sig[:64])
    if done is None:
        return _plain_page(i18n.newsletter_text(_page_lang(request), "unsubscribe_invalid"), 400, _page_lang(request))
    return _plain_page(i18n.newsletter_text(done["language"], "unsubscribed", name=done["name"] or "ATLAS"), lang=i18n.lang(done["language"]))


# ---------------------------------------------------------------- billing


class CheckoutRequest(BaseModel):
    plan: Literal["investor", "research_pro", "pro"]


@api.get("/billing")
def billing_summary(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import billing

    return billing.summary(p)


def _billing_call(fn, *args) -> dict[str, Any]:
    from atlas import billing

    try:
        return {"url": fn(*args)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except billing.NotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except billing.BillingError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/billing/checkout")
def billing_checkout(req: CheckoutRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    from atlas import billing

    return _billing_call(billing.checkout, p, req.plan)


@api.post("/billing/portal")
def billing_portal(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import billing

    return _billing_call(billing.portal, p)


@app.post(f"{V1}/billing/webhook", include_in_schema=False)
async def stripe_webhook(request: Request) -> dict[str, Any]:
    """Stripe events. Only signed payloads are accepted; each event is applied once."""
    import json

    from atlas import billing

    payload = await request.body()
    if not billing.verify_signature(payload, request.headers.get("stripe-signature"), os.getenv("STRIPE_WEBHOOK_SECRET", "")):
        raise HTTPException(400, "invalid signature")
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise HTTPException(400, "invalid payload") from exc
    return {"received": True, "result": billing.handle_event(event)}


# ---------------------------------------------------------------- two-factor authentication


class CodeRequest(BaseModel):
    code: str = Field(..., max_length=20)


class DisableTwoFactorRequest(BaseModel):
    password: str = Field(..., max_length=500)
    code: str = Field(..., max_length=20)


def _session_only(p: Principal) -> None:
    if p.via != "session":
        raise HTTPException(400, "manage two-factor authentication from a signed-in browser session")


@api.post("/account/2fa/setup")
def two_factor_setup(p: Principal = Depends(principal)) -> dict[str, Any]:
    _session_only(p)
    try:
        return auth.totp_begin(p)
    except auth.ValidationError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/account/2fa/enable")
def two_factor_enable(req: CodeRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    _session_only(p)
    try:
        codes = auth.totp_enable(p, req.code, keep_session=request.cookies.get(SESSION_COOKIE))
    except auth.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"enabled": True, "recovery_codes": codes}


@api.post("/account/2fa/disable", status_code=204)
def two_factor_disable(req: DisableTwoFactorRequest, request: Request, p: Principal = Depends(principal)) -> None:
    _session_only(p)
    try:
        auth._throttle(f"2fa-off|{p.user_id}|{request.client.host if request.client else ''}")
        auth.totp_disable(p, req.password, req.code)
    except auth.ValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(403, str(exc)) from exc


# ---------------------------------------------------------------- team


class InviteRequest(BaseModel):
    email: str = Field(..., max_length=320)
    role: Literal["admin", "member"] = "member"


class RoleRequest(BaseModel):
    role: Literal["admin", "member"]


class SwitchRequest(BaseModel):
    workspace_id: str = Field(..., max_length=32)


def _team_call(fn, *args):
    from atlas import teams

    try:
        return fn(*args)
    except teams.Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except auth.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except auth.QuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except KeyError:
        _404("member")


class TeamSettings(BaseModel):
    require_2fa: bool


@api.patch("/team/settings")
def team_settings(req: TeamSettings, p: Principal = Depends(principal)) -> dict[str, Any]:
    try:
        auth.set_require_2fa(p, req.require_2fa)
    except auth.AuthError as exc:
        raise HTTPException(403, str(exc)) from exc
    except auth.ValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"require_2fa": req.require_2fa}


@api.get("/team")
def team(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import teams

    acct = auth.account(p) if p.via != "local" else {}
    limits = auth.PLANS.get(acct["workspace"]["plan"], {}) if acct else {}
    return {"members": teams.members(p), "role": p.role, "require_2fa": bool(acct.get("workspace_requires_2fa")),
            "invitations": teams.invitations(p) if p.role in teams.MANAGERS else [],
            "seats": {"used": auth.usage(p.workspace_id)["seats"], "limit": limits.get("seats")}}


@api.post("/team/invitations", status_code=201)
def invite(req: InviteRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    from atlas import teams

    return _team_call(teams.invite, p, req.email, req.role)


@api.delete("/team/invitations/{invitation_id}", status_code=204)
def revoke_invitation(invitation_id: str, p: Principal = Depends(principal)) -> None:
    from atlas import teams

    if not _team_call(teams.revoke_invitation, p, invitation_id):
        _404("invitation")


@api.patch("/team/members/{user_id}", status_code=204)
def change_role(user_id: str, req: RoleRequest, p: Principal = Depends(principal)) -> None:
    from atlas import teams

    _team_call(teams.set_role, p, user_id, req.role)


@api.delete("/team/members/{user_id}", status_code=204)
def remove_member(user_id: str, p: Principal = Depends(principal)) -> None:
    from atlas import teams

    _team_call(teams.remove, p, user_id)


@api.post("/auth/switch-workspace")
def switch_workspace(req: SwitchRequest, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import teams

    session = request.cookies.get(SESSION_COOKIE)
    if p.via != "session" or not session:
        raise HTTPException(400, "switching workspaces needs a signed-in session")
    _team_call(teams.switch_workspace, session, p.user_id, req.workspace_id)
    return auth.account(auth.authenticate(session))


@api.get("/activity")
def activity(category: Literal["signin", "team", "sharing", "data", "keys", "security", "integrations", "newsletter"] | None = None,
             cursor: str | None = Query(None, max_length=80), limit: int = Query(100, ge=1, le=500), format: Literal["json", "csv"] = "json",
             p: Principal = Depends(principal)):
    """This workspace's audit log, newest first. Owners and admins only."""
    from atlas import activity as act

    if p.role not in ("owner", "admin"):
        raise HTTPException(403, "only workspace owners and admins can view activity")
    try:
        page = act.events(p.workspace_id, category=category, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(422, "invalid cursor") from exc
    if format == "csv":
        return Response(act.to_csv(page["events"]), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="atlas-activity-{date.today().isoformat()}.csv"'})
    return page


# ---------------------------------------------------------------- research


class ResearchScope(BaseModel):
    assets: list[str] = Field(default_factory=list)
    geographies: list[str] = Field(default_factory=list)
    lookback: str | None = None          # e.g. "5Y"
    forecast_horizon: str | None = None  # e.g. "3M"


class ResearchReportOptions(BaseModel):
    template: str = "research_report"
    formats: list[Literal["html", "pdf", "xlsx"]] = Field(default_factory=lambda: ["html", "xlsx"])
    include_sources: bool = True


class CustomScenario(BaseModel):
    driver: str = Field(..., max_length=64, description="Dataset id that moves, e.g. FX.USDBRL or RATE.UST.2Y.")
    shock: float = Field(..., ge=-500, le=500, description="% for price-like drivers, bp for rates.")
    name: str = Field("", max_length=120)


class ScenarioOptions(BaseModel):
    universe: list[str] = Field(default_factory=list, max_length=15, description="Markets to assess (empty: a cross-asset default).")
    custom: list[CustomScenario] = Field(default_factory=list, max_length=5)


def _scenario_config(opts: ScenarioOptions | None) -> dict[str, Any]:
    if opts is None:
        return {}
    unknown = [d for d in opts.universe + [c.driver for c in opts.custom] if not get_dataset(d)]
    if unknown:
        raise HTTPException(422, f"unknown datasets: {unknown}")
    out: dict[str, Any] = {}
    if opts.universe:
        out["scenario_universe"] = list(dict.fromkeys(opts.universe))
    if opts.custom:
        out["custom_scenarios"] = [c.model_dump() for c in opts.custom]
    return out


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=2000)
    depth: Literal["quick", "standard", "deep"] = "standard"
    engine: Literal["auto", "llm", "deterministic"] = "auto"
    scope: ResearchScope = Field(default_factory=ResearchScope)
    analysis: dict[str, bool] = Field(default_factory=dict)
    report: ResearchReportOptions = Field(default_factory=ResearchReportOptions)
    no_cache: bool = Field(False, description="Always run fresh research, even if an identical recent answer exists.")
    parent_run_id: str | None = Field(None, description="Ask a follow-up to this completed run in the same workspace.")
    language: Literal["en", "es", "pt"] = Field("en", description="Language of the brief and report (Claude engine; the deterministic engine writes English).")
    scenarios: ScenarioOptions | None = Field(None, description="For the scenario_outlook template: markets to assess and your own scenarios.")


def _parse_period(text: str | None, unit_months: dict[str, float]) -> float | None:
    if not text:
        return None
    text = text.strip().upper()
    try:
        return float(text[:-1]) * unit_months[text[-1]]
    except (KeyError, ValueError):
        raise HTTPException(422, f"cannot parse period '{text}' (use e.g. 5Y, 3M)")


@api.get("/report-templates")
def report_templates() -> list[dict[str, Any]]:
    from atlas.research.templates import list_templates

    return list_templates()


@api.post(f"/research", status_code=202)
def create_research(req: ResearchRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    from atlas.research.templates import TEMPLATES

    if req.report.template not in TEMPLATES:
        raise HTTPException(422, f"unknown template; choose one of {sorted(TEMPLATES)}")
    unknown = [a for a in req.scope.assets if not get_dataset(a)]
    if unknown:
        raise HTTPException(422, f"unknown assets: {unknown}; use /api/v1/instruments/search")
    lb = _parse_period(req.scope.lookback, {"Y": 1, "M": 1 / 12})
    hz = _parse_period(req.scope.forecast_horizon, {"Y": 12, "M": 1, "Q": 3})
    config = {
        "depth": req.depth, "engine": req.engine, "assets": req.scope.assets,
        "geographies": req.scope.geographies, "lookback_years": lb, "horizon_months": int(hz) if hz else None,
        "analysis": req.analysis, "formats": req.report.formats, "template": req.report.template,
        **({"language": req.language} if req.language != "en" else {}),
        **_scenario_config(req.scenarios),
    }
    if req.no_cache:
        config["cache"] = False
    if req.parent_run_id:
        parent = engine.get_run(req.parent_run_id, p.workspace_id) or _404("research run")
        if parent["status"] != "complete":
            raise HTTPException(409, "a follow-up needs a completed run")
        config["parent_run_id"] = req.parent_run_id

    def quotas() -> None:  # only charged when new research actually runs (cache hits are free)
        _quota(p.workspace_id, "research_per_day")
        if req.engine == "llm":
            _quota(p.workspace_id, "llm_research_per_day")
        elif req.engine == "auto" and llm.available():
            try:
                auth.check_quota(p.workspace_id, "llm_research_per_day")
            except auth.QuotaExceeded as exc:
                # "auto" means "best available": past the daily Claude limit the deterministic engine
                # answers, as it does when the monthly Claude budget runs out. An explicit "llm" is refused.
                config["engine"] = "deterministic"
                config["engine_note"] = f"Written without Claude: {exc}. The figures and their verification are unaffected."

    run_id, cached = engine.submit_or_cached(req.query, config, p.user_id, p.workspace_id, before_submit=quotas)
    return {"run_id": run_id, "status": "complete" if cached else "queued", "cached": cached,
            "engine_note": config.get("engine_note"), "links": {"self": f"{V1}/research/{run_id}"}}


@api.get(f"/research")
def list_research(limit: int = Query(50, ge=1, le=500), q: str | None = Query(None, max_length=200),
                  p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    return engine.list_runs(limit, p.workspace_id, q)


@api.delete("/research/{run_id}", status_code=204)
def delete_research(run_id: str, p: Principal = Depends(principal)) -> None:
    try:
        if not engine.delete_run(run_id, p.workspace_id, p.user_id, p.role):
            _404("research run")
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.get(f"/research/{{run_id}}")
def get_research(run_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    return engine.get_run(run_id, p.workspace_id) or _404("research run")


@api.post(f"/research/{{run_id}}/cancel", status_code=202)
def cancel_research(run_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    engine.get_run(run_id, p.workspace_id) or _404("research run")
    engine.cancel(run_id)
    return {"run_id": run_id, "status": "cancelling"}


# ---------------------------------------------------------------- quant


class QuantRequest(BaseModel):
    analysis: Literal["correlation", "regression", "anomalies", "forecast", "scenario", "seasonality", "performance", "regime", "event_study",
                      "fundamentals"]
    dataset_ids: list[str] = Field(default_factory=list)
    dependent: str | None = None
    explanatory: list[str] = Field(default_factory=list)
    asset: str | None = None
    driver: str | None = None
    shock: float | None = None
    lookback_years: float = 5
    horizon_months: int = 3
    threshold: float = 3.5
    event_type: Literal["fed_hike", "fed_cut", "fed_change", "custom"] = "fed_change"
    include_ml: bool = False
    dates: list[str] = Field(default_factory=list)
    stocks_basis: Literal["world", "world_ex_china"] | None = None


@api.post(f"/quant/analyze")
def quant_analyze(req: QuantRequest) -> dict[str, Any]:
    ctx = T.ToolContext()
    a = req.analysis
    calls: dict[str, tuple[str, dict[str, Any]]] = {
        "correlation": ("calculate_correlation", {"dataset_ids": req.dataset_ids, "lookback_years": req.lookback_years}),
        "regression": ("run_regression", {"dependent": req.dependent, "explanatory": req.explanatory, "lookback_years": req.lookback_years}),
        "anomalies": ("detect_anomalies", {"dataset_id": (req.dataset_ids or [None])[0], "threshold": req.threshold}),
        "forecast": ("run_forecast", {"dataset_id": (req.dataset_ids or [None])[0], "horizon_months": req.horizon_months,
                                      "include_ml": req.include_ml}),
        "scenario": ("run_scenario", {"asset": req.asset, "driver": req.driver, "shock": req.shock, "lookback_years": req.lookback_years}),
        "seasonality": ("get_seasonality", {"dataset_id": (req.dataset_ids or [None])[0]}),
        "performance": ("compare_performance", {"dataset_ids": req.dataset_ids}),
        "regime": ("detect_regime", {"dataset_id": (req.dataset_ids or [None])[0]}),
        "event_study": ("run_event_study", {"dataset_id": (req.dataset_ids or [None])[0], "event_type": req.event_type,
                                            **({"dates": req.dates} if req.dates else {})}),
        "fundamentals": ("get_fundamentals", {"commodity": (req.dataset_ids or [None])[0], "stocks_basis": req.stocks_basis}),
    }
    tool, args = calls[a]
    import json

    out, err = T.execute(ctx, tool, {k: v for k, v in args.items() if v is not None})
    body = json.loads(out)
    if err:
        raise HTTPException(422, body.get("error"))
    ev = ctx.evidence[-1]
    return {"analysis": a, "result": body, "citation": ev.citation, "sources": ev.sources, "chart": ev.chart}


# ---------------------------------------------------------------- reports


class ReportRequest(BaseModel):
    run_id: str
    formats: list[Literal["html", "pdf", "xlsx"]] = Field(default_factory=lambda: ["html", "xlsx"])
    template: str = "research_report"


@api.post(f"/reports", status_code=201)
def create_report(req: ReportRequest, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas.reporting import build_report

    engine.get_run(req.run_id, p.workspace_id) or _404("research run")
    try:
        return build_report(req.run_id, req.formats, req.template)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@api.get(f"/reports")
def list_reports(limit: int = 100, p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    from atlas.reporting import list_reports as lr

    return lr(limit, p.workspace_id)


@api.get(f"/reports/{{report_id}}")
def get_report(report_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas.reporting import get_report as gr

    r = gr(report_id, p.workspace_id) or _404("report")
    r.pop("paths", None)
    return r


@api.get(f"/reports/{{report_id}}/download")
def download_report(report_id: str, format: Literal["html", "xlsx", "pdf"] = "html", inline: bool = False,
                    p: Principal = Depends(principal)):
    from atlas.reporting import get_report as gr

    r = gr(report_id, p.workspace_id) or _404("report")
    fmt = format
    if fmt == "pdf" and "pdf" not in r["paths"]:
        fmt = "html"  # no PDF renderer installed: serve the print-ready HTML (print stylesheet included)
    path = r["paths"].get(fmt)
    if not path or not Path(path).exists():
        _404(f"{format} export")
    filename = f"atlas-report-{report_id[:8]}-v{r['version']}.{fmt}"
    return _report_file(path, fmt, filename, inline)


# Report HTML embeds model- and source-derived text; render it with no scripts in a sandboxed origin.
REPORT_HEADERS = {
    "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
}
REPORT_MEDIA = {"html": "text/html", "pdf": "application/pdf",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _report_file(path: str, fmt: str, filename: str, inline: bool, extra: dict[str, str] | None = None) -> FileResponse:
    headers = {**REPORT_HEADERS, **(extra or {})}
    if inline:
        headers["Content-Disposition"] = "inline"
    return FileResponse(path, media_type=REPORT_MEDIA[fmt], filename=None if inline else filename, headers=headers)


class ShareRequest(BaseModel):
    days: int = Field(7, ge=1, le=90)


@api.post("/reports/{report_id}/shares", status_code=201)
def share_report(report_id: str, req: ShareRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    from atlas import sharing

    try:
        return sharing.create(p, report_id, req.days)
    except KeyError:
        _404("report")


@api.get("/reports/{report_id}/shares")
def report_shares(report_id: str, p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    from atlas import sharing

    return sharing.list_shares(p, report_id)


@api.delete("/shares/{share_id}", status_code=204)
def revoke_share(share_id: str, p: Principal = Depends(principal)) -> None:
    from atlas import sharing

    if not sharing.revoke(p, share_id):
        _404("share link")


@app.get("/share/{token}", include_in_schema=False)
def view_shared_report(token: str, request: Request, format: Literal["html", "xlsx", "pdf"] = "html"):
    """Public, read-only view of one report through an unexpired, unrevoked share link."""
    from atlas import sharing

    r = sharing.resolve(token)
    if r is None:
        try:  # slow down token guessing; the response stays a plain 404 either way
            auth._throttle(f"share|{request.client.host if request.client else ''}")
        except auth.AuthError:
            raise HTTPException(429, "too many requests")
        raise HTTPException(404, "link not found or expired")
    fmt = "html" if format == "pdf" and "pdf" not in r["paths"] else format
    path = r["paths"].get(fmt)
    if not path or not Path(path).exists():
        raise HTTPException(404, "format not available")
    return _report_file(path, fmt, f"atlas-report-{r['report_id'][:8]}-v{r['version']}.{fmt}", inline=fmt == "html",
                        extra={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"})


# ---------------------------------------------------------------- scheduled reports


class ScheduleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    template: str = "research_report"
    query: str = Field(..., min_length=3, max_length=2000)
    cadence: Literal["daily", "weekly", "monthly"] = "weekly"
    depth: Literal["quick", "standard", "deep"] = "standard"
    engine: Literal["auto", "llm", "deterministic"] = "auto"
    formats: list[Literal["html", "pdf", "xlsx"]] = Field(default_factory=lambda: ["html", "xlsx"])
    scenarios: ScenarioOptions | None = None  # for the scenario_outlook template
    newsletter: Literal["off", "draft", "send"] = "off"  # turn each finished run into a newsletter issue


@api.post(f"/schedules", status_code=201)
def create_schedule(req: ScheduleRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    from atlas import schedules

    _quota(p.workspace_id, "schedules")
    if req.newsletter != "off":
        _check_newsletter_mode(p)
    return schedules.create_schedule(req.name, req.query, req.cadence,
                                     {"depth": req.depth, "engine": req.engine, "formats": req.formats, "template": req.template,
                                      **_scenario_config(req.scenarios),
                                      "newsletter": req.newsletter},
                                     workspace_id=p.workspace_id, user_id=p.user_id)


def _check_newsletter_mode(p: Principal) -> None:
    from atlas import newsletters

    if p.role not in ("owner", "admin"):
        raise HTTPException(403, "only workspace owners and admins can feed the newsletter")
    if newsletters.get_settings(p.workspace_id) is None:
        raise HTTPException(422, "set up the newsletter first (Newsletter page)")


class ScheduleNewsletter(BaseModel):
    newsletter: Literal["off", "draft", "send"]


@api.patch(f"/schedules/{{schedule_id}}")
def update_schedule(schedule_id: str, req: ScheduleNewsletter, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import schedules

    if req.newsletter != "off":
        _check_newsletter_mode(p)
    out = schedules.set_newsletter_mode(schedule_id, p.workspace_id, req.newsletter)
    if out is None:
        _404("schedule")
    auth.audit("schedule.newsletter_mode", p.user_id, p.workspace_id, schedule_id=schedule_id, mode=req.newsletter)
    return out


@api.get(f"/schedules")
def list_schedules(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    from atlas import schedules

    return schedules.list_schedules(p.workspace_id)


@api.post(f"/schedules/{{schedule_id}}/run", status_code=202)
def run_schedule_now(schedule_id: str, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import schedules

    try:
        _quota(p.workspace_id, "research_per_day")
        return {"run_id": schedules.run_schedule(schedule_id, workspace_id=p.workspace_id)}
    except KeyError:
        _404("schedule")


@api.delete(f"/schedules/{{schedule_id}}", status_code=204)
def delete_schedule(schedule_id: str, p: Principal = Depends(principal)) -> None:
    from atlas import schedules

    if not schedules.delete_schedule(schedule_id, p.workspace_id):
        _404("schedule")


# ---------------------------------------------------------------- portfolios


class PortfolioRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    holdings: list[dict[str, Any]]


@api.get("/portfolios")
def list_portfolios(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    from atlas import portfolios

    return portfolios.list_portfolios(p.workspace_id)


@api.post("/portfolios", status_code=201)
def create_portfolio(req: PortfolioRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    from atlas import portfolios

    try:
        return portfolios.save(req.name, req.holdings, p.workspace_id, p.user_id)
    except portfolios.PortfolioError as exc:
        raise HTTPException(422, str(exc)) from exc


@api.put("/portfolios/{portfolio_id}")
def update_portfolio(portfolio_id: str, req: PortfolioRequest, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import portfolios

    try:
        return portfolios.save(req.name, req.holdings, p.workspace_id, p.user_id, portfolio_id)
    except KeyError:
        _404("portfolio")
    except portfolios.PortfolioError as exc:
        raise HTTPException(422, str(exc)) from exc


@api.delete("/portfolios/{portfolio_id}", status_code=204)
def delete_portfolio(portfolio_id: str, p: Principal = Depends(principal)) -> None:
    from atlas import portfolios

    if not portfolios.delete(portfolio_id, p.workspace_id):
        _404("portfolio")


@api.get("/portfolios/{portfolio_id}/risk")
def portfolio_risk(portfolio_id: str, lookback_years: float = 5, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import portfolios

    port = portfolios.get(portfolio_id, p.workspace_id) or _404("portfolio")
    try:
        res, metas = portfolios.analyze(port["holdings"], lookback_years)
    except (portfolios.PortfolioError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"portfolio": port, "risk": res, "sources": [m.citation for m in metas]}


# ---------------------------------------------------------------- countries & positioning


@api.get(f"/countries")
def list_countries() -> dict[str, Any]:
    from atlas.instruments import countries, country_indicators

    return {"countries": countries(), "indicators": country_indicators()}


@api.get(f"/countries/compare")
def compare_countries_endpoint(indicators: str = "GDP_GROWTH,INFLATION,UNEMPLOYMENT", group: str | None = None,
                               countries: str | None = None) -> dict[str, Any]:
    import json

    args: dict[str, Any] = {"indicators": [i for i in indicators.split(",") if i]}
    if group:
        args["group"] = group
    if countries:
        args["countries"] = [c for c in countries.split(",") if c]
    ctx = T.ToolContext()
    out, err = T.execute(ctx, "compare_countries", args)
    if err:
        raise HTTPException(422, json.loads(out).get("error"))
    ev = ctx.evidence[-1]
    return {"result": json.loads(out), "chart": ev.chart, "sources": ev.sources}


@api.get(f"/trade/pairs")
def trade_pairs_endpoint() -> list[dict[str, Any]]:
    from atlas.instruments import trade_dataset_id, trade_pairs

    status = {r["dataset_id"]: r for r in access.data_status() if r["kind"] == "trade"}
    return [
        {"reporter": a, "partner": b, "dataset_id": trade_dataset_id(a, b),
         "name": get_dataset(trade_dataset_id(a, b)).name,  # type: ignore[union-attr]
         "observation_date": status.get(trade_dataset_id(a, b), {}).get("observation_date"),
         "freshness": status.get(trade_dataset_id(a, b), {}).get("freshness")}
        for a, b in trade_pairs()
    ]


@api.get(f"/trade")
def trade(reporter: str, partner: str = "WLD", top_n: int = 10) -> dict[str, Any]:
    import json

    ctx = T.ToolContext()
    out, err = T.execute(ctx, "get_trade_flows", {"reporter": reporter, "partner": partner, "top_n": top_n})
    if err:
        raise HTTPException(404 if "not loaded" in out else 409, json.loads(out).get("error"))
    ev = ctx.evidence[-1]
    return {"result": json.loads(out), "chart": ev.chart, "citation": ev.citation}


@api.get(f"/positioning")
def positioning(id: str, lookback_years: float = 3) -> dict[str, Any]:
    import json

    ctx = T.ToolContext()
    out, err = T.execute(ctx, "get_positioning", {"dataset_id": id, "lookback_years": lookback_years})
    if err:
        raise HTTPException(404 if "No CFTC" in out else 409, json.loads(out).get("error"))
    ev = ctx.evidence[-1]
    return {"result": json.loads(out), "chart": ev.chart, "citation": ev.citation}


@api.get("/fundamentals")
def fundamentals(id: str, country: str | None = None) -> dict[str, Any]:
    """USDA supply-and-demand balance for a commodity (price instrument id, FUND.* id or name)."""
    import json

    from atlas.instruments import fundamentals_for

    if fundamentals_for(id) is None:
        raise HTTPException(404, f"no supply-and-demand data for '{id}'")
    ctx = T.ToolContext()
    out, err = T.execute(ctx, "get_fundamentals", {"commodity": id, **({"geography": country} if country else {})})
    if err:
        raise HTTPException(409, json.loads(out).get("error"))
    ev = ctx.evidence[-1]
    return {"result": json.loads(out), "chart": ev.chart, "citation": ev.citation}


# ---------------------------------------------------------------- monitoring agents


class AgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    watch: list[str] = Field(..., max_length=20)
    triggers: list[dict[str, Any]] = Field(..., max_length=10)
    action: dict[str, Any]
    cooldown_hours: int = Field(24, ge=1, le=24 * 30)
    notify_email: bool = False


@api.get("/agents/triggers")
def agent_triggers() -> dict[str, Any]:
    from atlas.agents import TRIGGERS

    return TRIGGERS


@api.get("/agents")
def list_agents(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import agents

    return {"agents": agents.list_agents(p.workspace_id), "unread": agents.unread_count(p.workspace_id)}


@api.post("/agents", status_code=201)
def create_agent(req: AgentRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    from atlas import agents

    _quota(p.workspace_id, "agents")
    try:
        return agents.create(req.name, req.watch, req.triggers, req.action, req.cooldown_hours, req.notify_email,
                             p.workspace_id, p.user_id)
    except agents.AgentError as exc:
        raise HTTPException(422, str(exc)) from exc


@api.post("/agents/inbox/read", status_code=204)
def mark_agent_events_read(p: Principal = Depends(principal)) -> None:
    from atlas import agents

    agents.mark_read(p.workspace_id)


@api.post("/agents/{agent_id}/check")
def check_agent(agent_id: str, force: bool = False, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import agents

    if not agents.get_owned(agent_id, p.workspace_id):
        _404("agent")
    _quota(p.workspace_id, "research_per_day")
    return agents.check_agent(agent_id, force=force, manual=True)


@api.post("/agents/{agent_id}/{action}", status_code=204)
def set_agent_status(agent_id: str, action: Literal["pause", "resume"], p: Principal = Depends(principal)) -> None:
    from atlas import agents

    if not agents.set_status(agent_id, p.workspace_id, "paused" if action == "pause" else "active"):
        _404("agent")


@api.delete("/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str, p: Principal = Depends(principal)) -> None:
    from atlas import agents

    if not agents.set_status(agent_id, p.workspace_id, "deleted"):
        _404("agent")


# ---------------------------------------------------------------- alerts


class AlertRequest(BaseModel):
    name: str
    rule: dict[str, Any]
    schedule: str = "on_ingest"
    notify_email: bool = False


class AlertUpdate(BaseModel):
    notify_email: bool


@api.post(f"/alerts", status_code=201)
def create_alert(req: AlertRequest, p: Principal = Depends(require_verified)) -> dict[str, Any]:
    from atlas.intelligence import validate_rule

    _quota(p.workspace_id, "alerts")
    try:
        validate_rule(req.rule)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with session_scope() as s:
        a = Alert(name=req.name, rule=req.rule, schedule=req.schedule, workspace_id=p.workspace_id, user_id=p.user_id,
                  notify_email=req.notify_email)
        s.add(a)
        s.flush()
        return {"alert_id": a.alert_id, "name": a.name, "rule": a.rule, "status": a.status, "notify_email": bool(a.notify_email)}


@api.patch("/alerts/{alert_id}")
def update_alert(alert_id: str, req: AlertUpdate, p: Principal = Depends(principal)) -> dict[str, Any]:
    with session_scope() as s:
        a = s.get(Alert, alert_id)
        if a is None or a.workspace_id != p.workspace_id or a.status == "deleted":
            _404("alert")
        if req.notify_email and a.user_id != p.user_id:
            raise HTTPException(403, "only the alert's creator can turn on email for it (emails go to the creator)")
        a.notify_email = req.notify_email
        return {"alert_id": a.alert_id, "notify_email": bool(a.notify_email)}


@api.get(f"/alerts")
def list_alerts(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    with session_scope() as s:
        alerts = s.execute(select(Alert).where(Alert.workspace_id == p.workspace_id).order_by(Alert.created_at.desc())).scalars().all()
        ids = [a.alert_id for a in alerts]
        events = s.execute(select(AlertEvent).where(AlertEvent.alert_id.in_(ids))
                           .order_by(AlertEvent.triggered_at.desc()).limit(500)).scalars().all() if ids else []
        by_alert: dict[str, list[dict[str, Any]]] = {}
        for e in events:
            by_alert.setdefault(e.alert_id, []).append({"triggered_at": e.triggered_at.isoformat(timespec="seconds") + "Z", "message": e.message})
        return [{
            "alert_id": a.alert_id, "name": a.name, "rule": a.rule, "status": a.status,
            "last_evaluated_at": a.last_evaluated_at.isoformat(timespec="seconds") + "Z" if a.last_evaluated_at else None,
            "last_triggered_at": a.last_triggered_at.isoformat(timespec="seconds") + "Z" if a.last_triggered_at else None,
            "events": by_alert.get(a.alert_id, [])[:10],
            "notify_email": bool(a.notify_email), "mine": a.user_id == p.user_id,
        } for a in alerts]


@api.post(f"/alerts/evaluate")
def evaluate_alerts_now(p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas.intelligence import evaluate_alerts

    return {"fired": evaluate_alerts(None, workspace_id=p.workspace_id)}


@api.delete(f"/alerts/{{alert_id}}", status_code=204)
def delete_alert(alert_id: str, p: Principal = Depends(principal)) -> None:
    with session_scope() as s:
        a = s.get(Alert, alert_id)
        if a is None or a.workspace_id != p.workspace_id:
            _404("alert")
        a.status = "deleted"


app.include_router(api, prefix=V1)
