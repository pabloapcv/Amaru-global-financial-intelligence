"""Typed research tools (spec §8.2).

The LLM chooses tools; the tools do every calculation deterministically. Each successful
data/calculation call is recorded as an evidence item (E1, E2, ...) that the narrative must
cite and that the verification layer checks numbers against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import numpy as np
import pandas as pd

from atlas import analytics
from atlas.data.access import DataUnavailable, data_status, get_series, latest_quote, series_meta
from atlas.instruments import datasets, get_dataset, resolve


class ToolError(ValueError):
    """Raised for invalid tool input; returned to the model as an error tool_result."""


@dataclass
class Evidence:
    label: str
    tool: str
    args: dict[str, Any]
    result: dict[str, Any]
    citation: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    chart: dict[str, Any] | None = None  # chart-ready data kept out of the LLM view


@dataclass
class ToolContext:
    evidence: list[Evidence] = field(default_factory=list)
    calls: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def add(self, tool: str, args: dict[str, Any], result: dict[str, Any], citation: str,
            sources: list[dict[str, Any]], chart: dict[str, Any] | None = None) -> str:
        label = f"E{len(self.evidence) + 1}"
        self.evidence.append(Evidence(label, tool, args, result, citation, sources, chart))
        return label


# ---------------------------------------------------------------- helpers


def _ds(dataset_id: str):
    ds = get_dataset(dataset_id)
    if ds is None:
        hits = resolve(dataset_id, limit=3)
        hint = f" Did you mean: {', '.join(h['dataset_id'] for h in hits)}?" if hits else ""
        raise ToolError(f"Unknown dataset_id '{dataset_id}'. Use search_instruments first.{hint}")
    return ds


def _source(meta) -> dict[str, Any]:
    return {
        "dataset_id": meta.dataset_id,
        "name": meta.name,
        "citation": meta.citation,
        "url": meta.source_url,
        "observation_through": meta.last_observation,
        "ingested_at": meta.last_ingested_at,
        "freshness": meta.freshness,
        "price_basis": meta.price_basis,
    }


def _cite(metas) -> str:
    return "; ".join(
        f"{m.citation} — observations through {m.last_observation}, ingested {m.last_ingested_at}" for m in metas
    )


def _load(dataset_ids: list[str], lookback_years: float | None = None):
    series, metas = {}, []
    start = None
    if lookback_years:
        start = (pd.Timestamp.today() - pd.DateOffset(months=int(lookback_years * 12) + 3)).date()
    for dsid in dataset_ids:
        _ds(dsid)
        try:
            s, m = get_series(dsid, start=start)
        except DataUnavailable as exc:
            raise ToolError(f"DATA UNAVAILABLE — {exc}. Disclose this gap; do not estimate the value.") from exc
        series[dsid] = s
        metas.append(m)
    return series, metas


def _downsample(s: pd.Series, max_points: int = 400) -> list[dict[str, Any]]:
    if len(s) > max_points:
        step = int(np.ceil(len(s) / max_points))
        s = pd.concat([s.iloc[::step], s.iloc[[-1]]])
        s = s[~s.index.duplicated(keep="last")]
    return [{"date": d.date().isoformat(), "value": round(float(v), 6)} for d, v in s.items()]


def _lookback(args: dict[str, Any], default: float = 5) -> float:
    lb = float(args.get("lookback_years", default))
    if not 0.25 <= lb <= 40:
        raise ToolError("lookback_years must be between 0.25 and 40")
    return lb


# ---------------------------------------------------------------- tool implementations


def t_search_instruments(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    q = str(args.get("query", "")).strip()
    if q:
        hits = resolve(q, limit=8)
    else:
        hits = [
            {"dataset_id": d.dataset_id, "kind": d.kind, "name": d.name, "has_source": d.has_source,
             "frequency": d.frequency, "unit": d.unit}
            for d in datasets().values()
        ]
    return {"candidates": hits, "note": "Datasets with has_source=false are registered but have no data source configured."}


def t_get_data_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ids = args.get("dataset_ids") or []
    rows = [r for r in data_status() if not ids or r["dataset_id"] in ids]
    label = ctx.add("get_data_status", args, {"datasets": rows}, "ATLAS data status registry (dataset_state, pipeline_runs)", [])
    return {"evidence_id": label, "datasets": rows}


def t_get_latest_quote(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    dsid = args["dataset_id"]
    _ds(dsid)
    try:
        q = latest_quote(dsid)
    except DataUnavailable as exc:
        raise ToolError(f"DATA UNAVAILABLE — {exc}. Disclose this gap; do not estimate the value.") from exc
    meta = series_meta(dsid)
    label = ctx.add("get_latest_quote", args, q, _cite([meta]), [_source(meta)])
    return {"evidence_id": label, **q}


def _history(ctx: ToolContext, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    dsid = args.get("dataset_id") or args.get("series_id")
    ds = _ds(dsid)
    lb = _lookback(args)
    series, metas = _load([dsid], lookback_years=None)
    s = series[dsid]
    window = s[s.index > s.index[-1] - pd.DateOffset(months=int(lb * 12))]
    perf = analytics.performance(s, unit=ds.unit, frequency=ds.frequency)
    result: dict[str, Any] = {
        "dataset_id": dsid,
        "name": ds.name,
        "unit": ds.unit,
        "frequency": ds.frequency,
        "price_basis": metas[0].price_basis,
        "freshness": metas[0].freshness,
        "window_start": window.index[0].date().isoformat(),
        "window_end": window.index[-1].date().isoformat(),
        "window_min": round(float(window.min()), 6),
        "window_max": round(float(window.max()), 6),
        "window_mean": round(float(window.mean()), 6),
        "performance": perf,
    }
    if ds.kind == "macro" and ds.frequency in ("monthly", "quarterly") and "index" in ds.unit.lower():
        lag = 12 if ds.frequency == "monthly" else 4
        yoy = (s / s.shift(lag) - 1).dropna() * 100
        result["yoy_pct_recent"] = [
            {"period": d.date().isoformat(), "yoy_pct": round(float(v), 3)} for d, v in yoy.iloc[-6:].items()
        ]
    elif ds.kind == "macro" and ds.frequency == "quarterly":
        qoq_ann = ((s / s.shift(1)) ** 4 - 1).dropna() * 100
        result["qoq_annualized_pct_recent"] = [
            {"period": d.date().isoformat(), "pct": round(float(v), 3)} for d, v in qoq_ann.iloc[-6:].items()
        ]
    elif ds.kind == "macro" and "thousands" in ds.unit.lower():
        mom = s.diff().dropna()
        result["monthly_change_recent"] = [
            {"period": d.date().isoformat(), "change": round(float(v), 1)} for d, v in mom.iloc[-6:].items()
        ]
    # A compact sample for the model; the full window is kept for charts.
    result["sampled_points"] = _downsample(window, max_points=24)
    chart = {"type": "line", "title": ds.name, "unit": ds.unit, "series": [{"name": ds.name, "points": _downsample(window)}]}
    label = ctx.add(tool, args, result, _cite(metas), [_source(m) for m in metas], chart)
    return {"evidence_id": label, **result}


def t_get_price_history(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if _ds(args["dataset_id"]).kind != "market":
        raise ToolError("get_price_history is for market instruments; use get_macro_series for economic series")
    return _history(ctx, "get_price_history", args)


def t_get_macro_series(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if _ds(args["series_id"]).kind != "macro":
        raise ToolError("get_macro_series is for economic series; use get_price_history for market instruments")
    return _history(ctx, "get_macro_series", args)


def _meta_maps(ids: list[str]):
    return ({i: get_dataset(i).frequency for i in ids}, {i: get_dataset(i).unit for i in ids})  # type: ignore[union-attr]


def t_compare_performance(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ids = list(dict.fromkeys(args.get("dataset_ids") or []))
    if not ids:
        ac = args.get("asset_class")
        ids = [d.dataset_id for d in datasets().values() if d.has_source and (not ac or d.meta.get("asset_class") == ac)]
    rows, metas, gaps = [], [], []
    for dsid in ids:
        ds = _ds(dsid)
        try:
            s, m = get_series(dsid)
        except DataUnavailable as exc:
            gaps.append({"dataset_id": dsid, "reason": str(exc)})
            continue
        p = analytics.performance(s, unit=ds.unit, frequency=ds.frequency)
        metas.append(m)
        rows.append({
            "dataset_id": dsid, "name": ds.name, "unit": ds.unit, "frequency": ds.frequency,
            "last_value": p["last_value"], "last_date": p["last_date"],
            "trailing": {k: (v.get("return_pct") if v and "return_pct" in v else (v.get("change_bp") if v else None))
                         for k, v in p["trailing"].items()},
            "trailing_unit": "pct" if analytics.transform_kind(ds.unit) == "log_return" else "bp",
            "annualized_vol_1y_pct": p["annualized_vol_1y_pct"],
            "percentile_in_5y_range": p["percentile_in_5y_range"],
            "freshness": m.freshness,
        })
    result = {"rows": rows, "unavailable": gaps,
              "method": {"note": "Trailing returns between observation dates; mixed frequencies are not directly comparable over short horizons."}}
    label = ctx.add("compare_performance", args, result, _cite(metas), [_source(m) for m in metas])
    return {"evidence_id": label, **result}


def t_calculate_correlation(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ids = list(dict.fromkeys(args["dataset_ids"]))
    if len(ids) < 2 or len(ids) > 6:
        raise ToolError("provide 2–6 dataset_ids")
    lb = _lookback(args)
    series, metas = _load(ids)
    freqs, units = _meta_maps(ids)
    try:
        res = analytics.correlation(series, freqs, units, lookback_years=lb)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    chart = {
        "type": "line", "title": f"Rolling correlation with {_ds(ids[0]).name}", "unit": "correlation",
        "series": [{"name": f"{p['a']} vs {p['b']}", "points": p["rolling_series"]}
                   for p in res["pairs"] if ids[0] in (p["a"], p["b"])],
    }
    view = {**res, "pairs": [{k: v for k, v in p.items() if k != "rolling_series"} for p in res["pairs"]]}
    label = ctx.add("calculate_correlation", args, view, "ATLAS quant engine (correlation) on: " + _cite(metas),
                    [_source(m) for m in metas], chart)
    return {"evidence_id": label, **view}


def t_run_regression(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    y = args["dependent"]
    xs = list(dict.fromkeys(args["explanatory"]))
    if not 1 <= len(xs) <= 5 or y in xs:
        raise ToolError("provide 1–5 explanatory dataset_ids distinct from the dependent variable")
    lb = _lookback(args)
    ids = [y, *xs]
    series, metas = _load(ids)
    freqs, units = _meta_maps(ids)
    try:
        res = analytics.regression(y, series, freqs, units, lookback_years=lb)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    label = ctx.add("run_regression", args, res, "ATLAS quant engine (HAC OLS) on: " + _cite(metas), [_source(m) for m in metas])
    return {"evidence_id": label, **res}


def t_detect_anomalies(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    dsid = args["dataset_id"]
    ds = _ds(dsid)
    series, metas = _load([dsid])
    try:
        res = analytics.anomalies(series[dsid], unit=ds.unit, frequency=ds.frequency, threshold=float(args.get("threshold", 3.5)))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    label = ctx.add("detect_anomalies", args, {"dataset_id": dsid, **res}, "ATLAS quant engine (robust z) on: " + _cite(metas), [_source(m) for m in metas])
    return {"evidence_id": label, "dataset_id": dsid, **res}


def t_screen_anomalies(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    ac = args.get("asset_class")
    thr = float(args.get("threshold", 3.0))
    rows, metas, skipped = [], [], []
    for d in datasets().values():
        if not d.has_source or (ac and d.meta.get("asset_class", "macro") != ac):
            continue
        try:
            s, m = get_series(d.dataset_id)
            r = analytics.anomalies(s, unit=d.unit, frequency=d.frequency, threshold=thr)
        except (DataUnavailable, ValueError) as exc:
            skipped.append({"dataset_id": d.dataset_id, "reason": str(exc)})
            continue
        metas.append(m)
        rows.append({"dataset_id": d.dataset_id, "name": d.name, "frequency": d.frequency, **r["latest"]})
    rows.sort(key=lambda r: -abs(r["robust_z"] or 0))
    result = {"threshold": thr, "flagged": [r for r in rows if r["is_anomalous"]], "all_latest": rows, "skipped": skipped,
              "method": {"statistic": "robust z of latest period change vs trailing median/MAD"}}
    label = ctx.add("screen_anomalies", args, result, "ATLAS quant engine (anomaly screen) on: " + _cite(metas), [_source(m) for m in metas])
    return {"evidence_id": label, **result}


def t_get_seasonality(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    dsid = args["dataset_id"]
    ds = _ds(dsid)
    series, metas = _load([dsid])
    res = analytics.seasonality(series[dsid], unit=ds.unit, frequency=ds.frequency, years=int(args.get("years", 15)))
    label = ctx.add("get_seasonality", args, {"dataset_id": dsid, **res}, "ATLAS quant engine (seasonality) on: " + _cite(metas), [_source(m) for m in metas])
    return {"evidence_id": label, "dataset_id": dsid, **res}


def t_run_forecast(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    dsid = args["dataset_id"]
    ds = _ds(dsid)
    h = int(args.get("horizon_months", 3))
    if not 1 <= h <= 24:
        raise ToolError("horizon_months must be 1–24")
    series, metas = _load([dsid])
    try:
        res = analytics.forecast(series[dsid], unit=ds.unit, frequency=ds.frequency, horizon_months=h)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    s = series[dsid]
    hist = analytics.resample(s, "monthly", "mean") if ds.frequency in ("daily", "weekly") else s
    hist = hist[hist.index > hist.index[-1] - pd.DateOffset(years=5)]
    chart = {
        "type": "forecast", "title": f"{ds.name} — {analytics.METHOD_NAMES[res['selected_method']]} forecast", "unit": ds.unit,
        "series": [
            {"name": "history", "points": _downsample(hist)},
            {"name": "forecast", "points": [{"date": p["period"], "value": p["point"]} for p in res["path"]]},
        ],
        "band": [{"date": p["period"], "low": p.get("p10"), "high": p.get("p90")} for p in res["path"] if "p10" in p],
    }
    label = ctx.add("run_forecast", args, {"dataset_id": dsid, **res}, "ATLAS quant engine (walk-forward forecast) on: " + _cite(metas), [_source(m) for m in metas], chart)
    return {"evidence_id": label, "dataset_id": dsid, **res}


def t_run_scenario(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    asset, driver = args["asset"], args["driver"]
    if asset == driver:
        raise ToolError("asset and driver must differ")
    shock = float(args["shock"])
    lb = _lookback(args, default=10)
    series, metas = _load([asset, driver])
    freqs, units = _meta_maps([asset, driver])
    try:
        res = analytics.scenario(asset, driver, shock, series, freqs, units, lookback_years=lb)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    label = ctx.add("run_scenario", args, res, "ATLAS quant engine (historical-beta scenario) on: " + _cite(metas), [_source(m) for m in metas])
    return {"evidence_id": label, **res}


def _unavailable(kind: str, needed: str) -> Callable[[ToolContext, dict[str, Any]], dict[str, Any]]:
    def tool(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        result = {
            "status": "unavailable",
            "requested": args,
            "reason": f"No {kind} source is connected in this deployment. Requires: {needed}.",
            "instruction": "Report this as a data limitation. Do not substitute remembered or estimated values.",
        }
        label = ctx.add(f"{kind}_unavailable", args, result, f"ATLAS coverage registry: {kind} not connected", [])
        return {"evidence_id": label, **result}
    return tool


# ---------------------------------------------------------------- registry

_ID = {"type": "string", "description": "Canonical ATLAS dataset id, e.g. CMD.SUGAR.ISA or FX.USDBRL"}
_LB = {"type": "number", "description": "Years of history to analyse (default 5)."}

TOOLS: dict[str, dict[str, Any]] = {
    "search_instruments": {
        "fn": t_search_instruments,
        "description": "Resolve free text (e.g. 'sugar', 'Brazilian real', 'inflation') to canonical ATLAS dataset ids. Empty query lists every registered dataset. Does not create evidence.",
        "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    "get_data_status": {
        "fn": t_get_data_status,
        "description": "Freshness, observation dates and ingestion times for datasets. Use to disclose delayed/stale data.",
        "schema": {"type": "object", "properties": {"dataset_ids": {"type": "array", "items": {"type": "string"}}}, "required": []},
    },
    "get_latest_quote": {
        "fn": t_get_latest_quote,
        "description": "Latest stored observation with previous value, change, observation date, price basis and freshness. These are reference/benchmark prices, never live tradable quotes.",
        "schema": {"type": "object", "properties": {"dataset_id": _ID}, "required": ["dataset_id"]},
    },
    "get_price_history": {
        "fn": t_get_price_history,
        "description": "Historical market series summary: trailing returns (1M–5Y, YTD), 52w range, volatility, drawdowns, 5y percentile, sampled points.",
        "schema": {"type": "object", "properties": {"dataset_id": _ID, "lookback_years": _LB}, "required": ["dataset_id"]},
    },
    "get_macro_series": {
        "fn": t_get_macro_series,
        "description": "Economic series summary (point-in-time latest vintage): latest values, trailing changes, YoY for index series, recent prints.",
        "schema": {"type": "object", "properties": {"series_id": _ID, "lookback_years": _LB}, "required": ["series_id"]},
    },
    "compare_performance": {
        "fn": t_compare_performance,
        "description": "Cross-sectional table of trailing performance for many datasets (or every dataset in an asset_class: commodity, fx, rates, equity, volatility).",
        "schema": {"type": "object", "properties": {
            "dataset_ids": {"type": "array", "items": {"type": "string"}},
            "asset_class": {"type": "string", "enum": ["commodity", "fx", "rates", "equity", "volatility"]},
        }, "required": []},
    },
    "calculate_correlation": {
        "fn": t_calculate_correlation,
        "description": "Pearson/Spearman correlation of period changes for 2–6 datasets, aligned to the coarsest frequency, with 95% CI, rolling stability and lead/lag.",
        "schema": {"type": "object", "properties": {"dataset_ids": {"type": "array", "items": {"type": "string"}}, "lookback_years": _LB}, "required": ["dataset_ids"]},
    },
    "run_regression": {
        "fn": t_run_regression,
        "description": "OLS of the dependent dataset's changes on 1–5 explanatory datasets' changes, Newey-West HAC errors.",
        "schema": {"type": "object", "properties": {
            "dependent": _ID, "explanatory": {"type": "array", "items": {"type": "string"}}, "lookback_years": _LB,
        }, "required": ["dependent", "explanatory"]},
    },
    "detect_anomalies": {
        "fn": t_detect_anomalies,
        "description": "Robust z-score anomaly detection on one dataset's period changes; reports whether the latest move is unusual.",
        "schema": {"type": "object", "properties": {"dataset_id": _ID, "threshold": {"type": "number"}}, "required": ["dataset_id"]},
    },
    "screen_anomalies": {
        "fn": t_screen_anomalies,
        "description": "Screen every dataset (optionally one asset_class) for unusual latest moves, ranked by |robust z|.",
        "schema": {"type": "object", "properties": {
            "asset_class": {"type": "string", "enum": ["commodity", "fx", "rates", "equity", "volatility", "macro"]},
            "threshold": {"type": "number"},
        }, "required": []},
    },
    "get_seasonality": {
        "fn": t_get_seasonality,
        "description": "Average change by calendar month with hit rates and p-values.",
        "schema": {"type": "object", "properties": {"dataset_id": _ID, "years": {"type": "integer"}}, "required": ["dataset_id"]},
    },
    "run_forecast": {
        "fn": t_run_forecast,
        "description": "Walk-forward-evaluated statistical forecast vs naive baselines, with p10/p90 band. Reports whether it beats the baseline.",
        "schema": {"type": "object", "properties": {"dataset_id": _ID, "horizon_months": {"type": "integer"}}, "required": ["dataset_id"]},
    },
    "run_scenario": {
        "fn": t_run_scenario,
        "description": "Hypothetical shock: translate a driver move into an implied asset move via historical beta. shock is % for price-like drivers, bp for percent-unit drivers (e.g. -50 = 50bp cut).",
        "schema": {"type": "object", "properties": {
            "asset": _ID, "driver": _ID, "shock": {"type": "number"}, "lookback_years": _LB,
        }, "required": ["asset", "driver", "shock"]},
    },
    "get_fundamentals": {
        "fn": _unavailable("fundamentals", "USDA PSD / UNICA / ISO / ICSG supply-demand connectors"),
        "description": "Supply/demand fundamentals (production, stocks, crush, trade). Currently not connected — call it to record the gap.",
        "schema": {"type": "object", "properties": {"commodity": {"type": "string"}, "metric": {"type": "string"}, "geography": {"type": "string"}}, "required": ["commodity"]},
    },
    "get_futures_curve": {
        "fn": _unavailable("futures_curve", "a licensed ICE/CME settlement feed"),
        "description": "Futures term structure, spreads and open interest. Currently not connected — call it to record the gap.",
        "schema": {"type": "object", "properties": {"underlying": {"type": "string"}}, "required": ["underlying"]},
    },
    "search_documents": {
        "fn": _unavailable("documents", "a licensed news / research document index"),
        "description": "Search news and research documents. Currently not connected — call it to record the gap.",
        "schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
}


def anthropic_tool_specs() -> list[dict[str, Any]]:
    return [{"name": n, "description": t["description"], "input_schema": t["schema"]} for n, t in TOOLS.items()]


def _validate(name: str, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ToolError("tool input must be an object")
    schema = TOOLS[name]["schema"]
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError(f"missing required field(s): {', '.join(missing)}")
    unknown = [k for k in args if k not in schema["properties"]]
    if unknown:
        raise ToolError(f"unknown field(s): {', '.join(unknown)}")
    return args


def execute(ctx: ToolContext, name: str, args: Any) -> tuple[str, bool]:
    """Run a tool. Returns (JSON text for the model, is_error)."""
    ctx.calls += 1
    if name not in TOOLS:
        return json.dumps({"error": f"unknown tool {name}"}), True
    try:
        out = TOOLS[name]["fn"](ctx, _validate(name, args))
        ctx.log.append({"tool": name, "args": args, "ok": True, "evidence_id": out.get("evidence_id")})
        return json.dumps(out, default=str), False
    except (ToolError, KeyError) as exc:
        ctx.log.append({"tool": name, "args": args, "ok": False, "error": str(exc)})
        return json.dumps({"error": str(exc)}), True
