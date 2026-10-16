"""ATLAS REST API (spec §14). Run with `atlas serve` or `uvicorn atlas.api.main:app`."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from atlas import __version__, analytics, auth
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


app = FastAPI(title="ATLAS API", version=__version__, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"],
                   allow_credentials=True)
V1 = "/api/v1"
SESSION_COOKIE = "atlas_session"


def principal(request: Request) -> Principal:
    """Authenticate via session cookie or `Authorization: Bearer <session token | API key>`."""
    if not auth.auth_required():
        return auth.LOCAL_PRINCIPAL
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else request.cookies.get(SESSION_COOKIE)
    try:
        return auth.authenticate(token)
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc


def require_admin(p: Principal = Depends(principal)) -> Principal:
    if p.role not in ("owner", "admin") or p.workspace_id != auth.LOCAL_WORKSPACE and auth.auth_required():
        raise HTTPException(403, "operator access required")
    return p


def _quota(workspace_id: str, resource: str) -> None:
    try:
        auth.check_quota(workspace_id, resource)
    except auth.QuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc


api = APIRouter(dependencies=[Depends(principal)])


def _404(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


def _unavailable(exc: Exception):
    raise HTTPException(status_code=409, detail={"error": "data_unavailable", "message": str(exc)})


class SignupRequest(BaseModel):
    email: str
    password: str
    name: str = ""
    workspace_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


def _set_session(response: Response, token: str) -> None:
    secure = os.getenv("ATLAS_COOKIE_SECURE", "false").lower() == "true"
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=secure,
                        max_age=int(auth.SESSION_TTL.total_seconds()), path="/")


@app.post(f"{V1}/auth/signup", status_code=201)
def signup(req: SignupRequest, request: Request, response: Response) -> dict[str, Any]:
    if not auth.signup_allowed():
        raise HTTPException(403, "self-service signup is disabled")
    try:
        auth.create_user(req.email, req.password, req.name, req.workspace_name)
        token, p = auth.login(req.email, req.password, request.client.host if request.client else "")
    except auth.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    _set_session(response, token)
    return {"token": token, **auth.account(p)}


@app.post(f"{V1}/auth/login")
def login(req: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    try:
        token, p = auth.login(req.email, req.password, request.client.host if request.client else "")
    except auth.AuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    _set_session(response, token)
    return {"token": token, **auth.account(p)}


@app.post(f"{V1}/auth/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth.logout(token)
    response.delete_cookie(SESSION_COOKIE, path="/")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "llm_configured": llm.available(), "llm_model": settings.llm_model,
            "auth_required": auth.auth_required(), "signup_allowed": auth.signup_allowed()}


# ---------------------------------------------------------------- account


@api.get("/account")
def get_account(p: Principal = Depends(principal)) -> dict[str, Any]:
    return auth.account(p)


class ApiKeyRequest(BaseModel):
    name: str = Field("API key", max_length=200)


@api.get("/api-keys")
def list_api_keys(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    return auth.list_api_keys(p)


@api.post("/api-keys", status_code=201)
def create_api_key(req: ApiKeyRequest, p: Principal = Depends(principal)) -> dict[str, Any]:
    if p.via == "api_key":
        raise HTTPException(403, "API keys cannot create API keys; sign in")
    _quota(p.workspace_id, "api_keys")
    return auth.create_api_key(p, req.name)


@api.delete("/api-keys/{key_id}", status_code=204)
def revoke_api_key(key_id: str, p: Principal = Depends(principal)) -> None:
    if not auth.revoke_api_key(p, key_id):
        _404("API key")


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
            "coverage_note": d.meta.get("coverage_note"),
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


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=2000)
    depth: Literal["quick", "standard", "deep"] = "standard"
    engine: Literal["auto", "llm", "deterministic"] = "auto"
    scope: ResearchScope = Field(default_factory=ResearchScope)
    analysis: dict[str, bool] = Field(default_factory=dict)
    report: ResearchReportOptions = Field(default_factory=ResearchReportOptions)


def _parse_period(text: str | None, unit_months: dict[str, float]) -> float | None:
    if not text:
        return None
    text = text.strip().upper()
    try:
        return float(text[:-1]) * unit_months[text[-1]]
    except (KeyError, ValueError):
        raise HTTPException(422, f"cannot parse period '{text}' (use e.g. 5Y, 3M)")


@api.post(f"/research", status_code=202)
def create_research(req: ResearchRequest, p: Principal = Depends(principal)) -> dict[str, Any]:
    unknown = [a for a in req.scope.assets if not get_dataset(a)]
    if unknown:
        raise HTTPException(422, f"unknown assets: {unknown}; use /api/v1/instruments/search")
    lb = _parse_period(req.scope.lookback, {"Y": 1, "M": 1 / 12})
    hz = _parse_period(req.scope.forecast_horizon, {"Y": 12, "M": 1, "Q": 3})
    config = {
        "depth": req.depth, "engine": req.engine, "assets": req.scope.assets,
        "geographies": req.scope.geographies, "lookback_years": lb, "horizon_months": int(hz) if hz else None,
        "analysis": req.analysis, "formats": req.report.formats, "template": req.report.template,
    }
    _quota(p.workspace_id, "research_per_day")
    if req.engine == "llm" or (req.engine == "auto" and llm.available()):
        _quota(p.workspace_id, "llm_research_per_day")
    run_id = engine.submit(req.query, config, p.user_id, p.workspace_id)
    return {"run_id": run_id, "status": "queued", "links": {"self": f"{V1}/research/{run_id}"}}


@api.get(f"/research")
def list_research(limit: int = 50, p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    return engine.list_runs(limit, p.workspace_id)


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
    analysis: Literal["correlation", "regression", "anomalies", "forecast", "scenario", "seasonality", "performance", "regime"]
    dataset_ids: list[str] = Field(default_factory=list)
    dependent: str | None = None
    explanatory: list[str] = Field(default_factory=list)
    asset: str | None = None
    driver: str | None = None
    shock: float | None = None
    lookback_years: float = 5
    horizon_months: int = 3
    threshold: float = 3.5


@api.post(f"/quant/analyze")
def quant_analyze(req: QuantRequest) -> dict[str, Any]:
    ctx = T.ToolContext()
    a = req.analysis
    calls: dict[str, tuple[str, dict[str, Any]]] = {
        "correlation": ("calculate_correlation", {"dataset_ids": req.dataset_ids, "lookback_years": req.lookback_years}),
        "regression": ("run_regression", {"dependent": req.dependent, "explanatory": req.explanatory, "lookback_years": req.lookback_years}),
        "anomalies": ("detect_anomalies", {"dataset_id": (req.dataset_ids or [None])[0], "threshold": req.threshold}),
        "forecast": ("run_forecast", {"dataset_id": (req.dataset_ids or [None])[0], "horizon_months": req.horizon_months}),
        "scenario": ("run_scenario", {"asset": req.asset, "driver": req.driver, "shock": req.shock, "lookback_years": req.lookback_years}),
        "seasonality": ("get_seasonality", {"dataset_id": (req.dataset_ids or [None])[0]}),
        "performance": ("compare_performance", {"dataset_ids": req.dataset_ids}),
        "regime": ("detect_regime", {"dataset_id": (req.dataset_ids or [None])[0]}),
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
    fmt = "html" if format == "pdf" else format  # PDF = print the HTML (print stylesheet included)
    path = r["paths"].get(fmt)
    if not path or not Path(path).exists():
        _404(f"{format} export")
    media = {"html": "text/html", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
    filename = f"atlas-report-{report_id[:8]}-v{r['version']}.{fmt}"
    return FileResponse(path, media_type=media, filename=None if inline else filename,
                        headers={"Content-Disposition": "inline"} if inline else None)


# ---------------------------------------------------------------- scheduled reports


class ScheduleRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    query: str = Field(..., min_length=3, max_length=2000)
    cadence: Literal["daily", "weekly", "monthly"] = "weekly"
    depth: Literal["quick", "standard", "deep"] = "standard"
    engine: Literal["auto", "llm", "deterministic"] = "auto"
    formats: list[Literal["html", "pdf", "xlsx"]] = Field(default_factory=lambda: ["html", "xlsx"])


@api.post(f"/schedules", status_code=201)
def create_schedule(req: ScheduleRequest, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas import schedules

    _quota(p.workspace_id, "schedules")
    return schedules.create_schedule(req.name, req.query, req.cadence,
                                     {"depth": req.depth, "engine": req.engine, "formats": req.formats},
                                     workspace_id=p.workspace_id, user_id=p.user_id)


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


# ---------------------------------------------------------------- alerts


class AlertRequest(BaseModel):
    name: str
    rule: dict[str, Any]
    schedule: str = "on_ingest"


@api.post(f"/alerts", status_code=201)
def create_alert(req: AlertRequest, p: Principal = Depends(principal)) -> dict[str, Any]:
    from atlas.intelligence import validate_rule

    _quota(p.workspace_id, "alerts")
    try:
        validate_rule(req.rule)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with session_scope() as s:
        a = Alert(name=req.name, rule=req.rule, schedule=req.schedule, workspace_id=p.workspace_id, user_id=p.user_id)
        s.add(a)
        s.flush()
        return {"alert_id": a.alert_id, "name": a.name, "rule": a.rule, "status": a.status}


@api.get(f"/alerts")
def list_alerts(p: Principal = Depends(principal)) -> list[dict[str, Any]]:
    with session_scope() as s:
        alerts = s.execute(select(Alert).where(Alert.workspace_id == p.workspace_id).order_by(Alert.created_at.desc())).scalars().all()
        events = s.execute(select(AlertEvent).order_by(AlertEvent.triggered_at.desc()).limit(200)).scalars().all()
        by_alert: dict[str, list[dict[str, Any]]] = {}
        for e in events:
            by_alert.setdefault(e.alert_id, []).append({"triggered_at": e.triggered_at.isoformat(timespec="seconds") + "Z", "message": e.message})
        return [{
            "alert_id": a.alert_id, "name": a.name, "rule": a.rule, "status": a.status,
            "last_evaluated_at": a.last_evaluated_at.isoformat(timespec="seconds") + "Z" if a.last_evaluated_at else None,
            "last_triggered_at": a.last_triggered_at.isoformat(timespec="seconds") + "Z" if a.last_triggered_at else None,
            "events": by_alert.get(a.alert_id, [])[:10],
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
