"""ATLAS REST API (spec §14). Run with `atlas serve` or `uvicorn atlas.api.main:app`."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from atlas import __version__, analytics
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
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"], allow_headers=["*"])
V1 = "/api/v1"


def _404(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found")


def _unavailable(exc: Exception):
    raise HTTPException(status_code=409, detail={"error": "data_unavailable", "message": str(exc)})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "llm_configured": llm.available(), "llm_model": settings.llm_model}


# ---------------------------------------------------------------- reference & market data


@app.get(f"{V1}/instruments")
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


@app.get(f"{V1}/instruments/search")
def search_instruments(q: str = Query(..., min_length=1), limit: int = 8) -> list[dict[str, Any]]:
    return resolve(q, limit=limit)


@app.get(f"{V1}/markets/quotes")
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


@app.get(f"{V1}/markets/history")
def market_history(id: str, start: date | None = None, end: date | None = None, lookback_years: float | None = 5) -> dict[str, Any]:
    return _history(id, start, end, lookback_years)


@app.get(f"{V1}/macro/series")
def macro_series(id: str, start: date | None = None, end: date | None = None, lookback_years: float | None = 10,
                 as_of: str | None = Query(None, description="ISO datetime for a point-in-time (vintage) view")) -> dict[str, Any]:
    from datetime import datetime

    return _history(id, start, end, lookback_years, datetime.fromisoformat(as_of) if as_of else None)


@app.get(f"{V1}/futures/curve")
def futures_curve(underlying: str) -> JSONResponse:
    return JSONResponse(status_code=501, content={
        "error": "not_connected",
        "message": "Futures curves require a licensed ICE/CME settlement feed, which is not connected in this deployment.",
        "underlying": underlying,
    })


@app.get(f"{V1}/data/status")
def data_status() -> list[dict[str, Any]]:
    return access.data_status()


@app.get(f"{V1}/pipelines/status")
def pipelines_status(limit: int = 50) -> dict[str, Any]:
    return access.pipeline_status(limit)


class IngestRequest(BaseModel):
    dataset_ids: list[str] | None = None
    backfill: bool = False


@app.post(f"{V1}/pipelines/run", status_code=202)
def pipelines_run(req: IngestRequest) -> dict[str, Any]:
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


@app.post(f"{V1}/research", status_code=202)
def create_research(req: ResearchRequest) -> dict[str, Any]:
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
    run_id = engine.submit(req.query, config)
    return {"run_id": run_id, "status": "queued", "links": {"self": f"{V1}/research/{run_id}"}}


@app.get(f"{V1}/research")
def list_research(limit: int = 50) -> list[dict[str, Any]]:
    return engine.list_runs(limit)


@app.get(f"{V1}/research/{{run_id}}")
def get_research(run_id: str) -> dict[str, Any]:
    return engine.get_run(run_id) or _404("research run")


@app.post(f"{V1}/research/{{run_id}}/cancel", status_code=202)
def cancel_research(run_id: str) -> dict[str, Any]:
    engine.get_run(run_id) or _404("research run")
    engine.cancel(run_id)
    return {"run_id": run_id, "status": "cancelling"}


# ---------------------------------------------------------------- quant


class QuantRequest(BaseModel):
    analysis: Literal["correlation", "regression", "anomalies", "forecast", "scenario", "seasonality", "performance"]
    dataset_ids: list[str] = Field(default_factory=list)
    dependent: str | None = None
    explanatory: list[str] = Field(default_factory=list)
    asset: str | None = None
    driver: str | None = None
    shock: float | None = None
    lookback_years: float = 5
    horizon_months: int = 3
    threshold: float = 3.5


@app.post(f"{V1}/quant/analyze")
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


@app.post(f"{V1}/reports", status_code=201)
def create_report(req: ReportRequest) -> dict[str, Any]:
    from atlas.reporting import build_report

    try:
        return build_report(req.run_id, req.formats, req.template)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get(f"{V1}/reports")
def list_reports(limit: int = 100) -> list[dict[str, Any]]:
    from atlas.reporting import list_reports as lr

    return lr(limit)


@app.get(f"{V1}/reports/{{report_id}}")
def get_report(report_id: str) -> dict[str, Any]:
    from atlas.reporting import get_report as gr

    r = gr(report_id) or _404("report")
    r.pop("paths", None)
    return r


@app.get(f"{V1}/reports/{{report_id}}/download")
def download_report(report_id: str, format: Literal["html", "xlsx", "pdf"] = "html", inline: bool = False):
    from atlas.reporting import get_report as gr

    r = gr(report_id) or _404("report")
    fmt = "html" if format == "pdf" else format  # PDF = print the HTML (print stylesheet included)
    path = r["paths"].get(fmt)
    if not path or not Path(path).exists():
        _404(f"{format} export")
    media = {"html": "text/html", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
    filename = f"atlas-report-{report_id[:8]}-v{r['version']}.{fmt}"
    return FileResponse(path, media_type=media, filename=None if inline else filename,
                        headers={"Content-Disposition": "inline"} if inline else None)


# ---------------------------------------------------------------- alerts


class AlertRequest(BaseModel):
    name: str
    rule: dict[str, Any]
    schedule: str = "on_ingest"


@app.post(f"{V1}/alerts", status_code=201)
def create_alert(req: AlertRequest) -> dict[str, Any]:
    from atlas.intelligence import validate_rule

    try:
        validate_rule(req.rule)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    with session_scope() as s:
        a = Alert(name=req.name, rule=req.rule, schedule=req.schedule)
        s.add(a)
        s.flush()
        return {"alert_id": a.alert_id, "name": a.name, "rule": a.rule, "status": a.status}


@app.get(f"{V1}/alerts")
def list_alerts() -> list[dict[str, Any]]:
    with session_scope() as s:
        alerts = s.execute(select(Alert).order_by(Alert.created_at.desc())).scalars().all()
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


@app.post(f"{V1}/alerts/evaluate")
def evaluate_alerts_now() -> dict[str, Any]:
    from atlas.intelligence import evaluate_alerts

    return {"fired": evaluate_alerts(None)}


@app.delete(f"{V1}/alerts/{{alert_id}}", status_code=204)
def delete_alert(alert_id: str) -> None:
    with session_scope() as s:
        a = s.get(Alert, alert_id) or _404("alert")
        a.status = "deleted"
