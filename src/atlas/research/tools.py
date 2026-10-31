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
from atlas.data.access import (
    DataUnavailable,
    data_status,
    get_positioning,
    get_series,
    get_trade,
    latest_quote,
    series_meta,
)
from atlas.instruments import (
    countries,
    country_indicators,
    country_series_id,
    datasets,
    get_dataset,
    hs_chapters,
    positioning_for,
    resolve,
    trade_dataset_id,
    trade_pairs,
)


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
    workspace_id: str = "local"
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
        res = analytics.forecast(series[dsid], unit=ds.unit, frequency=ds.frequency, horizon_months=h,
                                 include_ml=bool(args.get("include_ml", False)))
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


def t_get_positioning(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    dsid = args["dataset_id"]
    ds = _ds(dsid)
    if ds.kind != "positioning":
        cot = positioning_for(dsid)
        if cot is None:
            raise ToolError(f"No CFTC positioning series is linked to {dsid}.")
        dsid = cot
    lb = _lookback(args, default=3)
    start = (pd.Timestamp.today() - pd.DateOffset(years=max(lb, 1) + 1)).date()
    try:
        df, meta = get_positioning(dsid, start=start)
    except DataUnavailable as exc:
        raise ToolError(f"DATA UNAVAILABLE — {exc}. Disclose this gap.") from exc
    stats = analytics.positioning_stats(df, lookback_years=lb)
    pds = get_dataset(dsid)
    result = {
        "dataset_id": dsid, "name": pds.name, "linked_instrument": pds.meta.get("instrument_id"),  # type: ignore[union-attr]
        "description": pds.meta.get("description"), "freshness": meta.freshness, **stats,  # type: ignore[union-attr]
    }
    window = df[df.index > df.index[-1] - pd.DateOffset(months=int(lb * 12))]
    chart = {
        "type": "line", "title": f"{pds.name} — net positions", "unit": "contracts",  # type: ignore[union-attr]
        "series": [
            {"name": "Non-commercial (speculators) net", "points": _downsample(window["noncomm_net"])},
            {"name": "Commercial (hedgers) net", "points": _downsample(window["comm_net"])},
        ],
    }
    label = ctx.add("get_positioning", args, result, _cite([meta]), [_source(meta)], chart)
    return {"evidence_id": label, **result}


def _country_codes(args: dict[str, Any]) -> list[str]:
    known = {c["iso3"]: c for c in countries()}
    codes = [str(c).upper() for c in args.get("countries") or []]
    group = args.get("group")
    if group:
        codes += [c["iso3"] for c in countries() if group in c["groups"] and c["iso3"] not in codes]
    bad = [c for c in codes if c not in known]
    if bad:
        raise ToolError(f"unknown country code(s) {bad}; available: {', '.join(sorted(known))}")
    if not codes:
        raise ToolError("provide countries (ISO3 codes) or a group ('latam' or 'g20')")
    return list(dict.fromkeys(codes))


def _indicator_keys(args: dict[str, Any], default: list[str]) -> list[str]:
    known = {i["key"]: i for i in country_indicators()}
    keys = [str(k).upper() for k in args.get("indicators") or default]
    bad = [k for k in keys if k not in known]
    if bad:
        raise ToolError(f"unknown indicator(s) {bad}; available: {', '.join(known)}")
    return keys


STALE_YEARS = 3


def t_compare_countries(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    codes = _country_codes(args)
    keys = _indicator_keys(args, ["GDP_GROWTH", "INFLATION", "UNEMPLOYMENT"])
    names = {c["iso3"]: c["name"] for c in countries()}
    ind_meta = {i["key"]: i for i in country_indicators()}
    rows, metas, gaps, history = [], [], [], {}
    for iso in codes:
        row: dict[str, Any] = {"country": iso, "name": names[iso]}
        for key in keys:
            sid = country_series_id(iso, key)
            try:
                s, m = get_series(sid)
            except DataUnavailable:
                row[key] = None
                gaps.append({"country": iso, "indicator": key, "reason": "no observations"})
                continue
            metas.append(m)
            last5 = s.iloc[-5:]
            row[key] = {"latest": round(float(s.iloc[-1]), 3), "year": int(s.index[-1].year),
                        "avg_5obs": round(float(last5.mean()), 3), "first_year_of_avg": int(last5.index[0].year)}
            history.setdefault(key, {})[iso] = s
        rows.append(row)
    # Flag values far older than the freshest peer for the same indicator; they are not comparable.
    stale = []
    for key in keys:
        newest = max((r[key]["year"] for r in rows if r.get(key)), default=None)
        for r in rows:
            if r.get(key):
                r[key]["stale"] = newest - r[key]["year"] > STALE_YEARS
                if r[key]["stale"]:
                    stale.append({"country": r["country"], "indicator": key, "year": r[key]["year"], "peer_latest_year": newest})
    ranks = {}
    for key in keys:
        vals = [(r["country"], r[key]["latest"]) for r in rows if r.get(key) and not r[key]["stale"]]
        ranks[key] = [c for c, _ in sorted(vals, key=lambda x: -x[1])]
    years = {r[k]["year"] for r in rows for k in keys if r.get(k)}
    result = {
        "indicators": {k: {"name": ind_meta[k]["name"], "unit": ind_meta[k]["unit"]} for k in keys},
        "rows": rows,
        "rank_high_to_low": ranks,
        "reference_years": sorted(years),
        "unavailable": gaps,
        "stale_values": stale,
        "method": {
            "source": "World Bank WDI, annual; latest available year per country (years can differ across countries).",
            "staleness": f"Values more than {STALE_YEARS} years older than the freshest peer are flagged stale and excluded from rankings.",
            "harmonization": "WDI definitions are harmonized across countries; inflation is CPI (annual %), unemployment is the ILO modeled estimate.",
            "caveat": "Annual data published with a lag; mixed reference years are not strictly comparable.",
        },
    }
    first = keys[0]
    chart = {
        "type": "line", "title": f"{ind_meta[first]['name']} by country", "unit": ind_meta[first]["unit"],
        "series": [
            {"name": names[iso], "points": _downsample(s[s.index.year >= s.index[-1].year - 15])}
            for iso, s in list(history.get(first, {}).items())[:12]
        ],
    }
    label = ctx.add("compare_countries", args, result, "; ".join(dict.fromkeys(m.citation for m in metas)) or "World Bank WDI",
                    [_source(m) for m in metas], chart)
    return {"evidence_id": label, **result}


def t_get_country_profile(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    iso = _country_codes({"countries": [args["country"]]})[0]
    names = {c["iso3"]: c["name"] for c in countries()}
    profile, metas, gaps, hist = {}, [], [], {}
    for ind in country_indicators():
        sid = country_series_id(iso, ind["key"])
        try:
            s, m = get_series(sid)
        except DataUnavailable:
            gaps.append(ind["key"])
            continue
        metas.append(m)
        recent = s.iloc[-6:]
        profile[ind["key"]] = {
            "name": ind["name"], "unit": ind["unit"],
            "recent": [{"year": int(d.year), "value": round(float(v), 3)} for d, v in recent.items()],
            "avg_10y": round(float(s.iloc[-10:].mean()), 3),
        }
        hist[ind["key"]] = s
    result = {"country": iso, "name": names[iso], "indicators": profile, "unavailable": gaps,
              "method": {"source": "World Bank WDI, annual", "caveat": "Latest year typically lags by 1–2 years."}}
    chart = {
        "type": "line", "title": f"{names[iso]}: growth and inflation", "unit": "percent",
        "series": [
            {"name": k.replace("_", " ").title(), "points": _downsample(hist[k][hist[k].index.year >= hist[k].index[-1].year - 20])}
            for k in ("GDP_GROWTH", "INFLATION") if k in hist
        ],
    }
    label = ctx.add("get_country_profile", args, result, "; ".join(m.citation for m in metas) or "World Bank WDI",
                    [_source(m) for m in metas], chart)
    return {"evidence_id": label, **result}


def t_get_trade_flows(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    rep, par = str(args["reporter"]).upper(), str(args.get("partner") or "WLD").upper()
    tid = trade_dataset_id(rep, par)
    if get_dataset(tid) is None:
        available = ", ".join(f"{a}->{b}" for a, b in trade_pairs())
        raise ToolError(f"Trade pair {rep}->{par} is not loaded. Available pairs: {available}. Disclose this gap.")
    try:
        df, meta = get_trade(tid)
    except DataUnavailable as exc:
        raise ToolError(f"DATA UNAVAILABLE — {exc}. Disclose this gap.") from exc
    top_n = int(args.get("top_n", 8))
    totals = df[df.cmd_code == "TOTAL"].pivot_table(index="year", columns="flow", values="value_usd", aggfunc="sum")
    by_year = []
    for year, row in totals.iterrows():
        x, m = row.get("X"), row.get("M")
        by_year.append({
            "year": int(year),
            "exports_usd": None if pd.isna(x) else round(float(x)),
            "imports_usd": None if pd.isna(m) else round(float(m)),
            "balance_usd": None if pd.isna(x) or pd.isna(m) else round(float(x - m)),
        })
    exp = totals.get("X", pd.Series(dtype=float)).dropna()
    growth = None
    if len(exp) >= 2 and exp.iloc[-2]:
        growth = round(float(exp.iloc[-1] / exp.iloc[-2] - 1) * 100, 2)
    cagr = None
    if len(exp) >= 4 and exp.iloc[0] > 0:
        years = int(exp.index[-1] - exp.index[0])
        cagr = round(float((exp.iloc[-1] / exp.iloc[0]) ** (1 / years) - 1) * 100, 2)
    names = hs_chapters()
    products = {}
    prod = df[df.cmd_code != "TOTAL"]
    for flow, label in (("X", "exports"), ("M", "imports")):
        part = prod[prod.flow == flow]
        if part.empty:
            continue
        year = int(part.year.max())
        part = part[part.year == year].sort_values("value_usd", ascending=False)
        total = float(totals.loc[year, flow]) if year in totals.index and flow in totals.columns else float(part.value_usd.sum())
        products[label] = {
            "year": year,
            "top": [
                {"hs_chapter": r.cmd_code, "description": names.get(r.cmd_code, ""), "value_usd": round(float(r.value_usd)),
                 "share_pct": round(float(r.value_usd) / total * 100, 2) if total else None}
                for r in part.head(top_n).itertuples()
            ],
            "concentration_top3_pct": round(float(part.value_usd.head(3).sum()) / total * 100, 2) if total else None,
        }
    ds = get_dataset(tid)
    result = {
        "dataset_id": tid, "name": ds.name, "reporter": rep, "partner": par,  # type: ignore[union-attr]
        "by_year": by_year, "latest_export_growth_pct": growth, "export_cagr_pct": cagr,
        "export_cagr_years": f"{int(exp.index[0])}-{int(exp.index[-1])}" if len(exp) >= 4 else None,
        "products": products, "freshness": meta.freshness,
        "method": {
            "source": "UN Comtrade, as declared by the reporter (exports FOB, imports typically CIF); partner 'WLD' = all partners.",
            "caveat": "Mirror statistics (the partner's declared imports) can differ materially; HS chapters are broad product groups. Latest year may be incomplete or revised.",
        },
    }
    chart = {
        "type": "line", "title": f"{ds.name}: exports vs imports", "unit": "USD",  # type: ignore[union-attr]
        "series": [
            {"name": "Exports", "points": [{"date": f"{r['year']}-01-01", "value": r["exports_usd"]} for r in by_year]},
            {"name": "Imports", "points": [{"date": f"{r['year']}-01-01", "value": r["imports_usd"]} for r in by_year]},
        ],
    }
    label = ctx.add("get_trade_flows", args, result, _cite([meta]), [_source(meta)], chart)
    return {"evidence_id": label, **result}


def t_detect_regime(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    dsid = args["dataset_id"]
    ds = _ds(dsid)
    series, metas = _load([dsid])
    try:
        res = analytics.volatility_regime(series[dsid], unit=ds.unit, frequency=ds.frequency, years=int(args.get("years", 10)))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    chart = {"type": "line", "title": f"{ds.name}: probability of high-volatility regime", "unit": "probability",
             "series": [{"name": "P(high-vol regime)", "points": res.pop("probability_series")}]}
    label = ctx.add("detect_regime", args, {"dataset_id": dsid, **res}, "ATLAS quant engine (Markov-switching volatility regime) on: " + _cite(metas),
                    [_source(m) for m in metas], chart)
    return {"evidence_id": label, "dataset_id": dsid, **res}


def t_analyze_portfolio(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas import portfolios

    holdings, name = args.get("holdings"), args.get("portfolio_name")
    if not holdings:
        saved = portfolios.find_by_name(name, ctx.workspace_id) if name else (portfolios.list_portfolios(ctx.workspace_id) or [None])[0]
        if saved is None:
            raise ToolError("No saved portfolio found in this workspace. Ask the user for holdings (dataset_id and weight).")
        holdings, name = saved["holdings"], saved["name"]
    try:
        res, metas = portfolios.analyze(holdings, float(args.get("lookback_years", 5)))
    except portfolios.PortfolioError as exc:
        raise ToolError(str(exc)) from exc
    chart = {"type": "line", "title": f"{name or 'Portfolio'}: cumulative value (start = 1)", "unit": "growth of 1",
             "series": [{"name": name or "Portfolio", "points": res.pop("cumulative")}]}
    result = {"portfolio_name": name, **res}
    label = ctx.add("analyze_portfolio", args, result, "ATLAS quant engine (portfolio risk) on: " + _cite(metas),
                    [_source(m) for m in metas], chart)
    return {"evidence_id": label, **result}


def t_run_event_study(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    dsid = args["dataset_id"]
    ds = _ds(dsid)
    if ds.frequency != "daily":
        raise ToolError(f"{dsid} is {ds.frequency}; event studies need daily data")
    etype = args.get("event_type", "fed_change")
    series, metas = _load([dsid])
    if etype == "custom":
        dates = [str(d) for d in args.get("dates") or []]
        if len(dates) < 3:
            raise ToolError("custom event studies need at least 3 dates (YYYY-MM-DD)")
        events = [{"date": d, "type": "custom"} for d in dates]
        label_events = "user-supplied dates"
    else:
        ff, ffm = _load(["RATE.US.FEDFUNDS"])
        metas += ffm
        events = analytics.policy_events(ff["RATE.US.FEDFUNDS"])
        if etype in ("fed_hike", "fed_cut"):
            events = [e for e in events if e["type"] == etype.removeprefix("fed_")]
        label_events = f"Fed policy-rate {etype.removeprefix('fed_').replace('change', 'changes')} inferred from the effective fed funds rate"
    try:
        res = analytics.event_study(series[dsid], [e["date"] for e in events], unit=ds.unit,
                                    pre=int(args.get("pre", 5)), post=int(args.get("post", 10)))
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    by_date = {e["date"]: e for e in events}
    for e in res["events"]:
        e.update({k: v for k, v in by_date.get(e["date"], {}).items() if k not in ("date",)})
    result = {"dataset_id": dsid, "name": ds.name, "event_type": etype, "event_source": label_events, **res}
    chart = {"type": "line", "title": f"{ds.name}: average cumulative abnormal move around {etype.replace('_', ' ')}s",
             "unit": res["unit"], "series": [{"name": "mean CAR", "points": [
                 {"date": (pd.Timestamp("2000-01-03") + pd.offsets.BDay(p["day"])).date().isoformat(), "value": p["mean_car"]}
                 for p in res["car_path"]]}],
             "x_is_event_time": True}
    label = ctx.add("run_event_study", args, result, "ATLAS quant engine (event study) on: " + _cite(metas),
                    [_source(m) for m in metas], chart)
    return {"evidence_id": label, **result}


def t_get_economic_calendar(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas import calendar

    days = int(args.get("days", 30))
    if not 1 <= days <= 400:
        raise ToolError("days must be between 1 and 400")
    cal = calendar.upcoming(days)
    if not cal["events"] and not cal["series_due"]:
        raise ToolError("DATA UNAVAILABLE — the economic calendar has not been fetched yet. Disclose this gap.")
    result = {k: cal[k] for k in ("from", "to", "series_due", "notes", "sources")}
    result["events"] = [{k: e[k] for k in ("date", "end_date", "kind", "title", "source")}
                        | ({"with_projections": True} if e["detail"].get("summary_of_economic_projections") else {})
                        | ({"datasets": [d["dataset_id"] for d in e["detail"].get("datasets", [])]} if e["kind"] == "data_release" else {})
                        for e in cal["events"]]
    names = {"fomc": "Federal Reserve Board, FOMC meeting calendar", "fred_releases": "FRED release calendar (Federal Reserve Bank of St. Louis)"}
    citation = "; ".join(f"{names.get(src, src)} — fetched {at}" for src, at in cal["sources"].items()) or "ATLAS calendar store"
    sources = [{"dataset_id": f"CALENDAR.{src.upper()}", "name": names.get(src, src), "citation": names.get(src, src),
                "url": calendar.FOMC_URL if src == "fomc" else "https://fred.stlouisfed.org/releases/calendar",
                "observation_through": None, "ingested_at": at, "freshness": "current", "price_basis": "schedule"}
               for src, at in cal["sources"].items()]
    label = ctx.add("get_economic_calendar", args, result, citation, sources)
    return {"evidence_id": label, **result}


def _price_vs_stocks(ds, df: pd.DataFrame, basis: str | None = None) -> tuple[dict[str, Any], list]:
    """Price implied by stocks-to-use (see analytics.price_vs_stocks_to_use); errors become a note.
    `basis` overrides the commodity's convention (world, or world excluding China)."""
    convention = ds.meta.get("price_model_basis", "world")
    basis = basis or convention
    sub = df[df["country_name"] != "China"] if basis == "world_ex_china" else df
    try:
        bal = analytics.fundamentals_balance(sub, unit=ds.unit, years=60)
        stu = {int(r["market_year"][:4]): r["stocks_to_use_pct"] for r in bal["balance"]}
        series, metas = _load([ds.meta["instrument_id"], "US.CPI.HEADLINE"])
        res = analytics.price_vs_stocks_to_use(series[ds.meta["instrument_id"]], series["US.CPI.HEADLINE"], stu,
                                               int(bal["latest_market_year"][:4]))
    except (ToolError, ValueError) as exc:
        return {"status": "unavailable", "reason": str(exc)}, []
    price_ds = _ds(ds.meta["instrument_id"])
    return {"price_dataset": price_ds.dataset_id, "price_unit": price_ds.unit,
            "stocks_basis": "world excluding China" if basis == "world_ex_china" else "world",
            "stocks_basis_is_convention": basis == convention,
            **({"stocks_basis_note": "China's grain, oilseed and cotton stocks are largely state-held and rarely reach the world market "
                                     "(USDA publishes world-less-China balances for the same reason)."} if basis == "world_ex_china" else {}),
            **res}, metas


def t_get_fundamentals(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas.data.access import get_fundamentals
    from atlas.instruments import fundamentals_for, load_registry

    fid = fundamentals_for(str(args["commodity"]))
    if fid is None:
        covered = ", ".join(c["name"].lower() for c in load_registry().get("fundamentals", {}).get("commodities", []))
        return _unavailable("fundamentals", f"a supply-demand source for {args['commodity']} (USDA PSD covers {covered}; "
                                            "energy and metals need EIA / ICSG / LME data)")(ctx, args)
    ds = _ds(fid)
    try:
        df, meta = get_fundamentals(fid)
    except DataUnavailable as exc:
        raise ToolError(f"DATA UNAVAILABLE — {exc}. Disclose this gap; do not estimate the value.") from exc
    try:
        res = analytics.fundamentals_balance(df, unit=ds.unit, country=args.get("geography") or None)
    except ValueError as exc:
        raise ToolError(f"{exc}. Use a country name as USDA spells it (e.g. Brazil, United States, European Union).") from exc
    years = res.pop("_chart_years")
    result = {"dataset_id": fid, "commodity": ds.meta["name"], **res}
    metas = [meta]
    if not args.get("geography") and ds.meta.get("instrument_id"):
        basis = args.get("stocks_basis")
        if basis not in (None, "world", "world_ex_china"):
            raise ToolError("stocks_basis must be 'world' or 'world_ex_china'")
        result["price_vs_stocks_to_use"], extra = _price_vs_stocks(ds, df, basis)
        metas += extra
    chart = {"type": "line", "title": f"{ds.meta['name']}: {res['geography'].lower() if res['geography'] == 'World' else res['geography']} production and consumption (USDA PSD)",
             "unit": ds.unit, "series": [
                 {"name": name, "points": [{"date": f"{y['market_year']}-01-01", "value": y[key]} for y in years if y[key] is not None]}
                 for name, key in (("Production", "production"), ("Consumption", "consumption"))]}
    label = ctx.add("get_fundamentals", args, result, _cite(metas) + f"; USDA report of {res['usda_release']}", [_source(m) for m in metas], chart)
    return {"evidence_id": label, **result}


def t_scan_scenarios(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas import scenarios

    custom = args.get("custom_scenarios") or []
    if not isinstance(custom, list) or len(custom) > 5 or any(not isinstance(c, dict) or "driver" not in c or "shock" not in c for c in custom):
        raise ToolError("custom_scenarios: up to 5 objects with driver (dataset id) and shock (% or bp)")
    saved = None
    portfolio_id, portfolio_name = args.get("portfolio_id"), args.get("portfolio_name")
    if portfolio_id or portfolio_name:
        from atlas import portfolios

        saved = portfolios.get(portfolio_id, ctx.workspace_id) if portfolio_id else portfolios.find_by_name(portfolio_name, ctx.workspace_id)
    try:
        res, metas = scenarios.scan(args.get("universe") or None, custom, int(args.get("max_scenarios", scenarios.MAX_SCENARIOS)), saved)
    except scenarios.ScenarioError as exc:
        raise ToolError(str(exc)) from exc
    res["portfolio_missing"] = bool((portfolio_id or portfolio_name) and not saved)  # e.g. deleted since a schedule was set up
    try:
        res["changes"] = scenarios.compare(scenarios.previous_scan(ctx.workspace_id), res)
    except Exception:  # the comparison is context; never fail the scan over it
        res["changes"] = None
    unique = list({m.dataset_id: m for m in metas}.values())
    label = ctx.add("scan_scenarios", args, res, "ATLAS scenario scan (rules on stored data; historical-beta effects) on: " + _cite(unique),
                    [_source(m) for m in unique])
    return {"evidence_id": label, **res}


def t_get_business_cycle(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas import cycle
    from atlas.data.access import series_meta

    try:
        full = cycle.outlook()
    except cycle.CycleUnavailable as exc:
        raise ToolError(f"DATA UNAVAILABLE — {exc}. Disclose this gap.") from exc
    r = full["recession"]
    res = {**full, "recession": {k: v for k, v in r.items() if k not in ("path", "recession_months")}}
    metas = [series_meta(d) for d in cycle.SERIES.values()]
    chart = {"type": "line", "title": "U.S. recession probability, next 12 months", "unit": "%",
             "series": [{"name": "Estimate made at the time", "points": r["path"]}]}
    label = ctx.add("get_business_cycle", args, res, "ATLAS business-cycle model on: " + _cite(metas), [_source(m) for m in metas], chart)
    return {"evidence_id": label, **res}


def t_get_policy_rates(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas import policy_rates

    try:
        full = policy_rates.overview(args.get("countries") or None)
    except policy_rates.PolicyRatesUnavailable as exc:
        raise ToolError(f"DATA UNAVAILABLE — {exc}. Disclose this gap.") from exc
    metas = full.pop("sources")
    path = full["direction"].pop("path")
    chart = {"type": "line", "title": "Central banks raising minus cutting over 6 months", "unit": "% of banks",
             "series": [{"name": "Raising minus cutting", "points": path}]}
    label = ctx.add("get_policy_rates", args, full, "ATLAS summary of BIS central bank policy rates: " + _cite(metas), [_source(m) for m in metas], chart)
    return {"evidence_id": label, **full}


def t_get_fertilizer_affordability(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas import fertilizer

    try:
        full = fertilizer.overview()
    except fertilizer.FertilizerUnavailable as exc:
        raise ToolError(f"DATA UNAVAILABLE — {exc}. Disclose this gap.") from exc
    metas = full.pop("sources")
    chart = {"type": "line", "title": "Tons of crop to buy a ton of fertilizer", "unit": "tons", "series": full.pop("chart")}
    label = ctx.add("get_fertilizer_affordability", args, full, "ATLAS ratio of fertilizer and crop prices: " + _cite(metas), [_source(m) for m in metas], chart)
    return {"evidence_id": label, **full}


def t_get_central_bank_tone(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas import tone

    limit = int(args.get("limit", 4))
    if not 1 <= limit <= 24:
        raise ToolError("limit must be between 1 and 24")
    bank = args.get("bank")
    if bank and bank not in tone.BANKS:
        raise ToolError(f"bank must be one of {sorted(tone.BANKS)}")
    res = tone.overview(limit)
    res["banks"] = [{**b, "statements": [s for s in b["statements"] if s["read"]]} for b in res["banks"] if not bank or b["bank"] == bank]
    read = [s for b in res["banks"] for s in b["statements"]]
    if not read:
        raise ToolError("DATA UNAVAILABLE — no central-bank statements have been read yet. Disclose this gap.")
    sources = [{"dataset_id": f"TONE.{s['bank'].upper()}.{s['meeting_date']}", "name": f"{s['bank_name']} statement, {s['meeting_date']}",
                "citation": f"{s['bank_name']}, {s['title']}, {s['meeting_date']}", "url": s["url"], "observation_through": s["meeting_date"],
                "ingested_at": None, "freshness": "current", "price_basis": "document"} for s in read]
    label = ctx.add("get_central_bank_tone", args, res, "Official Fed and ECB statements, tone as read by Claude; decisions checked against the text", sources)
    return {"evidence_id": label, **res}


def t_search_documents(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from atlas import documents

    days = int(args.get("days", 30))
    if not 1 <= days <= 730:
        raise ToolError("days must be between 1 and 730")
    res = documents.search(str(args.get("query", "")), days=days, limit=int(args.get("limit", 8)))
    if not res["indexed_sources"]:
        raise ToolError("DATA UNAVAILABLE — the document index has not been fetched yet. Disclose this gap.")
    pubs = {d["source"]: d["publisher"] for d in res["results"]}
    citation = "Official press-release feeds: " + ", ".join(f["publisher"] for f in documents.FEEDS if f["source"] in res["indexed_sources"])
    sources = [{"dataset_id": f"DOCS.{src.upper()}", "name": f"{pub} press releases", "citation": f"{pub}, press release feed",
                "url": next(f["url"] for f in documents.FEEDS if f["source"] == src), "observation_through": None,
                "ingested_at": None, "freshness": "current", "price_basis": "document"} for src, pub in pubs.items()]
    label = ctx.add("search_documents", args, res, citation, sources)
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
        "description": "Walk-forward-evaluated statistical forecast vs naive baselines, with p10/p90 band. Reports whether it beats the baseline. include_ml adds a gradient-boosting candidate, selected only if it wins the walk-forward test.",
        "schema": {"type": "object", "properties": {"dataset_id": _ID, "horizon_months": {"type": "integer"},
                                                    "include_ml": {"type": "boolean", "description": "Also evaluate a gradient-boosting model (slower)."}},
                   "required": ["dataset_id"]},
    },
    "run_scenario": {
        "fn": t_run_scenario,
        "description": "Hypothetical shock: translate a driver move into an implied asset move via historical beta. shock is % for price-like drivers, bp for percent-unit drivers (e.g. -50 = 50bp cut).",
        "schema": {"type": "object", "properties": {
            "asset": _ID, "driver": _ID, "shock": {"type": "number"}, "lookback_years": _LB,
        }, "required": ["asset", "driver", "shock"]},
    },
    "scan_scenarios": {
        "fn": t_scan_scenarios,
        "description": "Scenario scan across markets: proposes the scenarios current data points to (FOMC calendar and 2-year yield moves → rates; dollar and oil momentum or 5-year extremes; calm volatility → a VIX spike; CFTC positioning extremes → unwinds; USDA stocks-to-use extremes → squeezes or surpluses), plus custom scenarios, and gives each scenario's historical-beta effect on every market in the universe with 95% ranges and significance, plus what each market actually did in past episodes when the driver moved that much within 3 months. With portfolio_name, also the effect on that saved portfolio. Relevance counts signals; it is NOT a probability.",
        "schema": {"type": "object", "properties": {
            "universe": {"type": "array", "items": _ID, "description": "Markets to assess (default: a cross-asset set of 11). At most 15."},
            "custom_scenarios": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "driver": _ID, "shock": {"type": "number", "description": "% for price-like drivers, bp for rates"}},
                "required": ["driver", "shock"]}, "description": "Up to 5 scenarios to include regardless of signals."},
            "max_scenarios": {"type": "integer", "minimum": 1, "maximum": 8},
            "portfolio_name": {"type": "string", "description": "A saved portfolio to test each scenario against."},
            "portfolio_id": {"type": "string", "description": "The same, by id."},
        }},
    },
    "get_business_cycle": {
        "fn": t_get_business_cycle,
        "description": "U.S. business cycle: a 12-month recession probability from a probit on the yield spread, financial conditions and unemployment, with its walk-forward record (AUC, Brier score against always guessing the average) and a simpler model for comparison; plus the past months most like today and what markets did in the next 12 months. State the limits: very few recessions were tested, unemployment history is revised, and similar periods are a lookup, not a tested signal.",
        "schema": {"type": "object", "properties": {}},
    },
    "get_fertilizer_affordability": {
        "fn": t_get_fertilizer_affordability,
        "description": "How many tons of corn, wheat, rice, soybeans, sugar or coffee it takes to buy a ton of urea, DAP or potash, ranked against each pair's history. Uses world benchmark prices, not farm-gate costs; say so, and don't call a high reading a forecast.",
        "schema": {"type": "object", "properties": {}},
    },
    "get_policy_rates": {
        "fn": t_get_policy_rates,
        "description": "Central bank policy rates from the BIS for 19 currency areas (daily): each bank's rate, its latest move with the date, changes over 3 and 12 months, and whether it has been cutting, hiking or on hold over 6 months, plus how many banks are doing each worldwide. The euro area stands for Germany, France and Italy. Argentina, Bolivia, Ecuador, Uruguay and Paraguay are not covered.",
        "schema": {"type": "object", "properties": {
            "countries": {"type": "array", "items": {"type": "string"}, "description": "ISO3 codes to show (default: every bank). The worldwide count always covers every bank."},
        }},
    },
    "get_central_bank_tone": {
        "fn": t_get_central_bank_tone,
        "description": "Tone of recent Fed and ECB policy statements: the decision, a stance from -2 (much more dovish) to +2 (much more hawkish) compared with the previous statement, balance sheet, dissents (Fed) and a one-line summary. The stance is Claude's reading of the language, not a measurement or forecast; say so. Decisions are checked against the statement text, and 'consistent: false' flags a mismatch.",
        "schema": {"type": "object", "properties": {
            "bank": {"type": "string", "enum": ["fed", "ecb"], "description": "One bank (default: both)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 24, "description": "Statements per bank, newest first (default 4)."},
        }},
    },
    "get_positioning": {
        "fn": t_get_positioning,
        "description": "CFTC Commitments of Traders positioning for a market (pass the instrument id, e.g. CMD.SUGAR.ISA, or a COT.* id): speculator and hedger net positions, weekly/4-week changes, % of open interest, COT index (0–100 within the lookback range) and z-score. Works for gold and silver even though their prices are not connected.",
        "schema": {"type": "object", "properties": {"dataset_id": _ID, "lookback_years": {"type": "number", "description": "Range for COT index/z-score (default 3)."}}, "required": ["dataset_id"]},
    },
    "compare_countries": {
        "fn": t_compare_countries,
        "description": "Cross-country comparison of annual World Bank indicators (latest value, its year, 5-observation average, ranking). Indicators: GDP_GROWTH, INFLATION, UNEMPLOYMENT, GDP_PC, CURRENT_ACCOUNT, EXPORTS, RESERVES, DEBT. Countries by ISO3 code or group 'latam' / 'g20'.",
        "schema": {"type": "object", "properties": {
            "countries": {"type": "array", "items": {"type": "string"}},
            "group": {"type": "string", "enum": ["latam", "g20"]},
            "indicators": {"type": "array", "items": {"type": "string"}},
        }, "required": []},
    },
    "get_country_profile": {
        "fn": t_get_country_profile,
        "description": "Standardized economic profile of one country (ISO3 code): recent annual growth, inflation, unemployment, income, external accounts, reserves and debt from World Bank WDI.",
        "schema": {"type": "object", "properties": {"country": {"type": "string", "description": "ISO3 code, e.g. BRA, BOL"}}, "required": ["country"]},
    },
    "get_trade_flows": {
        "fn": t_get_trade_flows,
        "description": "Bilateral goods trade from UN Comtrade for a reporter country (ISO3) and partner (ISO3, or 'WLD' for all partners): annual exports/imports/balance, export growth and CAGR, top HS-chapter products with shares and concentration. Only pre-loaded pairs are available; the error lists them.",
        "schema": {"type": "object", "properties": {
            "reporter": {"type": "string", "description": "ISO3, e.g. BOL"},
            "partner": {"type": "string", "description": "ISO3, e.g. USA, or WLD for all partners"},
            "top_n": {"type": "integer"},
        }, "required": ["reporter"]},
    },
    "detect_regime": {
        "fn": t_detect_regime,
        "description": "Volatility regime for one dataset: realized-volatility percentile bucket (low/normal/elevated/stress) and a 2-state Markov-switching model's probability of the high-volatility regime now, regime volatilities, persistence and recent high-vol episodes.",
        "schema": {"type": "object", "properties": {"dataset_id": _ID, "years": {"type": "integer"}}, "required": ["dataset_id"]},
    },
    "analyze_portfolio": {
        "fn": t_analyze_portfolio,
        "description": "Risk of a portfolio: pass holdings [{dataset_id, weight}] (weights are fractions of capital, negative = short; price instruments only) or the name of a saved portfolio in the user's workspace (omit both for the most recent one). Returns volatility, VaR/CVaR, risk contributions, correlations, factor betas (dollar, 10Y yield, Brent) and historical stress tests.",
        "schema": {"type": "object", "properties": {
            "holdings": {"type": "array", "items": {"type": "object", "properties": {"dataset_id": {"type": "string"}, "weight": {"type": "number"}}, "required": ["dataset_id", "weight"]}},
            "portfolio_name": {"type": "string"},
            "lookback_years": {"type": "number"},
        }, "required": []},
    },
    "run_event_study": {
        "fn": t_run_event_study,
        "description": "Event study on a daily dataset: average cumulative abnormal move from day -pre to +post around events. event_type: fed_hike, fed_cut, fed_change (Fed decisions inferred from the effective fed funds rate since 2009) or custom with dates. Reports CAR path, reaction mean/median, hit rate, t-stat.",
        "schema": {"type": "object", "properties": {
            "dataset_id": _ID,
            "event_type": {"type": "string", "enum": ["fed_hike", "fed_cut", "fed_change", "custom"]},
            "dates": {"type": "array", "items": {"type": "string"}},
            "pre": {"type": "integer"}, "post": {"type": "integer"},
        }, "required": ["dataset_id"]},
    },
    "get_fundamentals": {
        "fn": t_get_fundamentals,
        "description": "USDA supply-and-demand balance sheet for an agricultural commodity (sugar, coffee, corn, wheat, soybeans, cotton, rice): production, consumption, trade, ending stocks and stocks-to-use for recent market years (world, or one country via geography), year-on-year changes, stocks-to-use versus 20 years, top producers/exporters and revisions since the previous USDA report. For world balances with a price series it also fits real price against stocks-to-use across market years and reports the price the USDA projection implies, an 80% band and where the latest price sits (association, not a forecast). The latest market year is a USDA projection. Other commodities return a recorded data gap.",
        "schema": {"type": "object", "properties": {
            "commodity": {"type": "string", "description": "Commodity name or price dataset id, e.g. 'sugar' or CMD.CORN.US."},
            "metric": {"type": "string", "description": "Optional focus (e.g. 'ending stocks'); the full balance is always returned."},
            "geography": {"type": "string", "description": "Optional country as USDA names it (e.g. Brazil); default world."},
            "stocks_basis": {"type": "string", "enum": ["world", "world_ex_china"],
                             "description": "Override the price model's stocks basis; default is the commodity's convention."},
        }, "required": ["commodity"]},
    },
    "get_futures_curve": {
        "fn": _unavailable("futures_curve", "a licensed ICE/CME settlement feed"),
        "description": "Futures term structure, spreads and open interest. Currently not connected — call it to record the gap.",
        "schema": {"type": "object", "properties": {"underlying": {"type": "string"}}, "required": ["underlying"]},
    },
    "get_economic_calendar": {
        "fn": t_get_economic_calendar,
        "description": "Scheduled events in the next `days` (default 30): FOMC meetings from the Federal Reserve Board's calendar and, when configured, official U.S. data release dates from FRED. Also lists the next reference period due for each tracked U.S. macro series. Never infer release dates that are not listed.",
        "schema": {"type": "object", "properties": {"days": {"type": "integer", "description": "Look-ahead window in days (1-400)."}}, "required": []},
    },
    "search_documents": {
        "fn": t_search_documents,
        "description": "Keyword search over official press releases (Federal Reserve Board, BEA, CFTC) from the last `days` (default 30); an empty query returns the latest releases. Returns titles, dates, links and summaries. No commercial news wire or sell-side research is connected: say so when the question needs news.",
        "schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords, e.g. 'GDP', 'discount rate', 'swap'."},
            "days": {"type": "integer", "description": "Look-back window in days (1-730)."},
            "limit": {"type": "integer", "description": "Maximum results (default 8)."},
        }, "required": ["query"]},
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
