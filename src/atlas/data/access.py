"""Unified Data Access Layer: asset resolution, canonical queries and freshness (spec §6).

Every series handed to the quant or research engines carries a `SeriesMeta` describing
where it came from, how fresh it is and what kind of price it is. Nothing downstream
should need to look at provider tickers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import case, func, select

from atlas.data.db import (
    DatasetState,
    MacroObservation,
    MarketObservation,
    PipelineRun,
    PositioningObservation,
    session_scope,
    utcnow,
)
from atlas.instruments import Dataset, datasets, get_dataset
from atlas.provider_sdk import ProviderAdapter, get_adapter


@lru_cache(maxsize=None)
def _citation_adapter(provider_id: str) -> ProviderAdapter:
    """Shared adapter used only for display names and citations (never for fetching)."""
    return get_adapter(provider_id)

# Max age of the latest observation (vs today) before a dataset counts as delayed.
# Public sources publish with a lag; these thresholds encode the *expected* lag.
EXPECTED_LAG = {"daily": timedelta(days=10), "weekly": timedelta(days=14), "monthly": timedelta(days=100),
                "quarterly": timedelta(days=200), "annual": timedelta(days=900)}
# Annual periods are dated Jan 1, so the annual lag covers the year itself plus ~1.5y of publication delay.


class DataUnavailable(LookupError):
    """Raised when a dataset has no stored observations; callers must disclose the gap."""


@dataclass
class SeriesMeta:
    dataset_id: str
    name: str
    kind: str
    unit: str
    frequency: str
    price_basis: str
    provider: str | None
    provider_symbol: str | None
    source_url: str | None
    citation: str | None
    first_observation: str | None
    last_observation: str | None
    last_ingested_at: str | None
    last_checked_at: str | None
    freshness: str
    freshness_note: str
    n_observations: int
    as_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _freshness(ds: Dataset, last_obs: date | None, state: DatasetState | None, today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    if not ds.has_source:
        return "no_source", ds.meta.get("coverage_note") or "No data source configured."
    if last_obs is None:
        if state and state.last_error:
            return "failing", f"Ingestion failing: {state.last_error}"
        return "no_data", "Not yet ingested."
    lag = EXPECTED_LAG.get(ds.frequency, timedelta(days=400))
    age = today - last_obs
    if state and state.consecutive_failures:
        return "failing", f"Last {state.consecutive_failures} refresh(es) failed: {state.last_error}"
    if age <= lag:
        return "current", f"Latest observation {last_obs.isoformat()} is within the expected publication lag for {ds.frequency} data."
    if age <= 2 * lag:
        return "delayed", f"Latest observation {last_obs.isoformat()} is older than the expected lag ({lag.days}d)."
    return "stale", f"Latest observation {last_obs.isoformat()} is {age.days} days old."


def series_meta(dataset_id: str) -> SeriesMeta:
    ds = get_dataset(dataset_id)
    if ds is None:
        raise KeyError(f"unknown dataset '{dataset_id}'")
    with session_scope() as s:
        if ds.kind == "market":
            q = select(
                func.min(MarketObservation.observed_at), func.max(MarketObservation.observed_at),
                func.max(MarketObservation.ingested_at), func.count(),
            ).where(MarketObservation.instrument_id == dataset_id, MarketObservation.field == "close")
        elif ds.kind == "positioning":
            q = select(
                func.min(PositioningObservation.report_date), func.max(PositioningObservation.report_date),
                func.max(PositioningObservation.ingested_at), func.count(),
            ).where(PositioningObservation.series_id == dataset_id)
        else:
            q = select(
                func.min(MacroObservation.period), func.max(MacroObservation.period),
                func.max(MacroObservation.vintage_at), func.count(func.distinct(MacroObservation.period)),
            ).where(MacroObservation.series_id == dataset_id)
        first, last, ingested, n = s.execute(q).one()
        state = s.get(DatasetState, dataset_id)
        freshness, note = _freshness(ds, last, state)
        adapter = _citation_adapter(ds.provider_id) if ds.has_source else None
        return SeriesMeta(
            dataset_id=dataset_id,
            name=ds.name,
            kind=ds.kind,
            unit=ds.unit,
            frequency=ds.frequency,
            price_basis=ds.meta.get("price_basis", "economic_indicator"),
            provider=adapter.display_name if adapter else None,
            provider_symbol=ds.provider_symbol,
            source_url=adapter.source_url(ds.provider_symbol) if adapter else None,  # type: ignore[arg-type]
            citation=adapter.citation(ds.provider_symbol) if adapter else None,  # type: ignore[arg-type]
            first_observation=first.isoformat() if first else None,
            last_observation=last.isoformat() if last else None,
            last_ingested_at=ingested.isoformat(timespec="seconds") + "Z" if ingested else None,
            last_checked_at=state.last_checked_at.isoformat(timespec="seconds") + "Z" if state and state.last_checked_at else None,
            freshness=freshness,
            freshness_note=note,
            n_observations=int(n or 0),
        )


def get_series(
    dataset_id: str,
    start: date | None = None,
    end: date | None = None,
    as_of: datetime | None = None,
) -> tuple[pd.Series, SeriesMeta]:
    """Return a dated float Series plus metadata.

    For macro series `as_of` gives a point-in-time view: only vintages ingested at or
    before `as_of` are visible, which prevents look-ahead bias in backtests.
    """
    ds = get_dataset(dataset_id)
    if ds is None:
        raise KeyError(f"unknown dataset '{dataset_id}'")
    meta = series_meta(dataset_id)
    with session_scope() as s:
        if ds.kind == "market":
            q = select(MarketObservation.observed_at, MarketObservation.value).where(
                MarketObservation.instrument_id == dataset_id, MarketObservation.field == "close"
            )
            if start:
                q = q.where(MarketObservation.observed_at >= start)
            if end:
                q = q.where(MarketObservation.observed_at <= end)
            if as_of:
                q = q.where(MarketObservation.observed_at <= as_of.date())
            rows = s.execute(q.order_by(MarketObservation.observed_at)).all()
        elif ds.kind == "positioning":
            # Primary value: net non-commercial (speculative) position in contracts.
            net = PositioningObservation.noncomm_long - PositioningObservation.noncomm_short
            q = select(PositioningObservation.report_date, net).where(PositioningObservation.series_id == dataset_id)
            if start:
                q = q.where(PositioningObservation.report_date >= start)
            if end:
                q = q.where(PositioningObservation.report_date <= end)
            if as_of:
                q = q.where(PositioningObservation.report_date <= as_of.date())
            rows = s.execute(q.order_by(PositioningObservation.report_date)).all()
        else:
            cutoff = as_of or utcnow() + timedelta(days=1)
            latest = (
                select(MacroObservation.period, func.max(MacroObservation.vintage_at).label("v"))
                .where(MacroObservation.series_id == dataset_id, MacroObservation.vintage_at <= cutoff)
                .group_by(MacroObservation.period)
                .subquery()
            )
            q = select(MacroObservation.period, MacroObservation.value).join(
                latest,
                (MacroObservation.period == latest.c.period) & (MacroObservation.vintage_at == latest.c.v),
            ).where(MacroObservation.series_id == dataset_id)
            if start:
                q = q.where(MacroObservation.period >= start)
            if end:
                q = q.where(MacroObservation.period <= end)
            rows = s.execute(q.order_by(MacroObservation.period)).all()
    if not rows:
        raise DataUnavailable(f"{dataset_id}: no stored observations ({meta.freshness}: {meta.freshness_note})")
    idx = pd.DatetimeIndex([r[0] for r in rows], name="date")
    series = pd.Series([float(r[1]) for r in rows], index=idx, name=dataset_id)
    if as_of:
        meta.as_of = as_of.isoformat()
    return series, meta


def get_positioning(dataset_id: str, start: date | None = None) -> tuple[pd.DataFrame, SeriesMeta]:
    """Full COT breakdown per report date, with derived net positions."""
    ds = get_dataset(dataset_id)
    if ds is None or ds.kind != "positioning":
        raise KeyError(f"'{dataset_id}' is not a positioning dataset")
    meta = series_meta(dataset_id)
    cols = ["open_interest", "noncomm_long", "noncomm_short", "noncomm_spread", "comm_long", "comm_short",
            "nonrept_long", "nonrept_short"]
    with session_scope() as s:
        q = select(PositioningObservation.report_date, *[getattr(PositioningObservation, c) for c in cols]).where(
            PositioningObservation.series_id == dataset_id
        )
        if start:
            q = q.where(PositioningObservation.report_date >= start)
        rows = s.execute(q.order_by(PositioningObservation.report_date)).all()
    if not rows:
        raise DataUnavailable(f"{dataset_id}: no stored observations ({meta.freshness}: {meta.freshness_note})")
    df = pd.DataFrame(rows, columns=["date", *cols]).set_index(pd.DatetimeIndex([r[0] for r in rows], name="date"))
    df = df.drop(columns="date").astype(float)
    df["noncomm_net"] = df["noncomm_long"] - df["noncomm_short"]
    df["comm_net"] = df["comm_long"] - df["comm_short"]
    df["nonrept_net"] = df["nonrept_long"] - df["nonrept_short"]
    return df, meta


def latest_quote(dataset_id: str) -> dict[str, Any]:
    series, meta = get_series(dataset_id, start=date.today() - timedelta(days=3 * 366))
    last_date = series.index[-1]
    out: dict[str, Any] = {
        "dataset_id": dataset_id,
        "name": meta.name,
        "value": round(float(series.iloc[-1]), 6),
        "unit": meta.unit,
        "observation_date": last_date.date().isoformat(),
        "price_basis": meta.price_basis,
        "freshness": meta.freshness,
        "freshness_note": meta.freshness_note,
        "ingested_at": meta.last_ingested_at,
        "source": meta.citation,
        "is_tradable_quote": False,  # no licensed real-time feed is connected in this deployment
    }
    if len(series) > 1:
        prev = float(series.iloc[-2])
        out["previous_value"] = round(prev, 6)
        out["previous_date"] = series.index[-2].date().isoformat()
        out["change"] = round(float(series.iloc[-1]) - prev, 6)
        out["change_pct"] = round((float(series.iloc[-1]) / prev - 1) * 100, 4) if prev else None
    return out


def data_status() -> list[dict[str, Any]]:
    out = []
    for dsid, ds in datasets().items():
        m = series_meta(dsid)
        out.append({
            "dataset_id": dsid,
            "name": ds.name,
            "kind": ds.kind,
            "asset_class": ds.meta.get("asset_class", "macro"),
            "frequency": ds.frequency,
            "provider_symbol": ds.provider_symbol,
            "observation_date": m.last_observation,
            "ingested_at": m.last_ingested_at,
            "checked_at": m.last_checked_at,
            "n_observations": m.n_observations,
            "freshness": m.freshness,
            "freshness_note": m.freshness_note,
        })
    return out


def pipeline_status(limit: int = 50) -> dict[str, Any]:
    with session_scope() as s:
        runs = s.execute(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit)).scalars().all()
        since = utcnow() - timedelta(days=30)
        total, ok = s.execute(
            select(func.count(), func.sum(case((PipelineRun.status == "success", 1), else_=0)))
            .where(PipelineRun.started_at >= since)
        ).one()
        return {
            "success_rate_30d": round((ok or 0) / total, 4) if total else None,
            "runs_30d": total,
            "recent_runs": [
                {
                    "execution_id": r.execution_id,
                    "dataset_id": r.dataset_id,
                    "status": r.status,
                    "started_at": r.started_at.isoformat(timespec="seconds") + "Z",
                    "finished_at": r.finished_at.isoformat(timespec="seconds") + "Z" if r.finished_at else None,
                    "rows_fetched": r.rows_fetched,
                    "rows_inserted": r.rows_inserted,
                    "rows_revised": r.rows_revised,
                    "findings": (r.quality_metrics or {}).get("findings", []),
                    "error": r.error,
                }
                for r in runs
            ],
        }
