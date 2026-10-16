"""Saved portfolios (spec FR-015): workspace-scoped holdings plus risk analysis."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from atlas import analytics
from atlas.data.access import DataUnavailable, get_series, series_meta
from atlas.data.db import LOCAL_WORKSPACE, Portfolio, session_scope, utcnow
from atlas.instruments import get_dataset

FACTORS = ["FX.USD.BROAD", "RATE.UST.10Y", "CMD.CRUDE.BRENT"]
MAX_HOLDINGS = 25


class PortfolioError(ValueError):
    pass


def validate_holdings(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not holdings:
        raise PortfolioError("a portfolio needs at least one holding")
    if len(holdings) > MAX_HOLDINGS:
        raise PortfolioError(f"at most {MAX_HOLDINGS} holdings")
    out, seen = [], set()
    for h in holdings:
        dsid, weight = str(h.get("dataset_id", "")), h.get("weight")
        ds = get_dataset(dsid)
        if ds is None or ds.kind != "market":
            raise PortfolioError(f"'{dsid}' is not a market instrument")
        if analytics.transform_kind(ds.unit) != "log_return":
            raise PortfolioError(f"'{dsid}' is quoted in {ds.unit}, not a price; it cannot be held directly")
        if not ds.has_source:
            raise PortfolioError(f"'{dsid}' has no price source configured")
        if not isinstance(weight, (int, float)) or not -5 <= weight <= 5 or weight == 0:
            raise PortfolioError(f"weight for '{dsid}' must be a non-zero number between -5 and 5")
        if dsid in seen:
            raise PortfolioError(f"'{dsid}' appears twice")
        seen.add(dsid)
        out.append({"dataset_id": dsid, "weight": float(weight)})
    return out


def _to_dict(p: Portfolio) -> dict[str, Any]:
    return {"portfolio_id": p.portfolio_id, "name": p.name, "holdings": p.holdings,
            "created_at": p.created_at.isoformat(timespec="seconds") + "Z",
            "updated_at": p.updated_at.isoformat(timespec="seconds") + "Z"}


def save(name: str, holdings: list[dict[str, Any]], workspace_id: str = LOCAL_WORKSPACE, user_id: str = "local",
         portfolio_id: str | None = None) -> dict[str, Any]:
    clean = validate_holdings(holdings)
    with session_scope() as s:
        if portfolio_id:
            p = s.get(Portfolio, portfolio_id)
            if p is None or p.workspace_id != workspace_id:
                raise KeyError(portfolio_id)
            p.name, p.holdings, p.updated_at = name, clean, utcnow()
        else:
            p = Portfolio(name=name, holdings=clean, workspace_id=workspace_id, user_id=user_id)
            s.add(p)
            s.flush()
        return _to_dict(p)


def list_portfolios(workspace_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        rows = s.execute(select(Portfolio).where(Portfolio.workspace_id == workspace_id)
                         .order_by(Portfolio.updated_at.desc())).scalars().all()
        return [_to_dict(p) for p in rows]


def get(portfolio_id: str, workspace_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        p = s.get(Portfolio, portfolio_id)
        return _to_dict(p) if p and p.workspace_id == workspace_id else None


def find_by_name(name: str, workspace_id: str) -> dict[str, Any] | None:
    for p in list_portfolios(workspace_id):
        if p["name"].strip().lower() == name.strip().lower():
            return p
    return None


def delete(portfolio_id: str, workspace_id: str) -> bool:
    with session_scope() as s:
        p = s.get(Portfolio, portfolio_id)
        if p is None or p.workspace_id != workspace_id:
            return False
        s.delete(p)
        return True


def analyze(holdings: list[dict[str, Any]], lookback_years: float = 5) -> tuple[dict[str, Any], list]:
    """Risk report for holdings; returns (result, series metadata for citations)."""
    clean = validate_holdings(holdings)
    weights = {h["dataset_id"]: h["weight"] for h in clean}
    series, metas, full = {}, [], {}
    for dsid in weights:
        try:
            s, m = get_series(dsid)
        except DataUnavailable as exc:
            raise PortfolioError(f"DATA UNAVAILABLE — {exc}") from exc
        full[dsid], series[dsid] = s, s
        metas.append(m)
    factors, f_units, f_freq = {}, {}, {}
    for f in FACTORS:
        if f in weights:
            continue  # a holding cannot also be a regressor
        try:
            fs, fm = get_series(f)
        except DataUnavailable:
            continue
        factors[f], f_units[f], f_freq[f] = fs, get_dataset(f).unit, get_dataset(f).frequency  # type: ignore[union-attr]
        metas.append(fm)
    res = analytics.portfolio_risk(
        series, weights, {i: get_dataset(i).frequency for i in weights},  # type: ignore[union-attr]
        factors, f_units, f_freq, lookback_years=lookback_years, full_history=full,
    )
    names = {i: get_dataset(i).name for i in weights}  # type: ignore[union-attr]
    for h in res["holdings"]:
        h["name"] = names[h["dataset_id"]]
        h["freshness"] = series_meta(h["dataset_id"]).freshness
    return res, metas
