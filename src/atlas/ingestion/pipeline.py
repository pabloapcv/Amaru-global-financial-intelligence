"""Ingestion pipeline: discover → preserve raw → validate → normalize → publish → trigger.

Implements spec §7. Each dataset run is idempotent: re-running with the same provider data
inserts nothing new. Raw payloads are stored before parsing so any batch can be replayed.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atlas.config import settings
from atlas.data.db import (
    DatasetState,
    MacroObservation,
    MarketObservation,
    PipelineRun,
    PositioningObservation,
    TradeFlow,
    RawPayload,
    session_scope,
    utcnow,
)
from atlas.ingestion import quality
from atlas.instruments import Dataset, datasets
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, get_adapter

log = logging.getLogger("atlas.ingestion")

PIPELINE_ID = "provider_ingest_v1"
# Re-fetch window behind the watermark, so late corrections/revisions are picked up.
OVERLAP = {"daily": timedelta(days=21), "weekly": timedelta(days=60), "monthly": timedelta(days=400),
           "quarterly": timedelta(days=800), "annual": timedelta(days=1500)}
NON_POSITIVE_OK = {"rates", "volatility"}


@dataclass
class DatasetResult:
    dataset_id: str
    status: str
    rows_fetched: int = 0
    rows_inserted: int = 0
    rows_revised: int = 0
    findings: list[dict[str, Any]] | None = None
    error: str | None = None
    execution_id: str | None = None


def _store_raw(provider_id: str, symbol: str, payload: bytes, content_type: str = "text/csv") -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
    ext = "json" if "json" in content_type else "csv"
    safe_symbol = symbol.replace(":", "_").replace("/", "_")
    path = Path(settings.object_store) / "raw" / provider_id / safe_symbol / f"{stamp}_{digest[:12]}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return str(path), digest


def _previous_values(session: Session, ds: Dataset) -> list[float]:
    if ds.kind == "market":
        rows = session.execute(
            select(MarketObservation.value)
            .where(MarketObservation.instrument_id == ds.dataset_id, MarketObservation.field == "close")
            .order_by(MarketObservation.observed_at.desc())
            .limit(60)
        ).scalars().all()
    elif ds.kind in ("positioning", "trade"):
        return []
    else:
        rows = session.execute(
            select(MacroObservation.value)
            .where(MacroObservation.series_id == ds.dataset_id)
            .order_by(MacroObservation.period.desc())
            .limit(60)
        ).scalars().all()
    return list(reversed(rows))


def _publish_market(session: Session, ds: Dataset, points: list[ParsedPoint], run_id: str, raw_id: str) -> tuple[int, int]:
    if not points:
        return 0, 0
    existing = {
        o.observed_at: o
        for o in session.execute(
            select(MarketObservation).where(
                MarketObservation.instrument_id == ds.dataset_id,
                MarketObservation.field == "close",
                MarketObservation.provider_id == ds.provider_id,
                MarketObservation.observed_at >= points[0].observed_at,
            )
        ).scalars()
    }
    inserted = revised = 0
    for p in points:
        cur = existing.get(p.observed_at)
        if cur is None:
            session.add(MarketObservation(
                instrument_id=ds.dataset_id, observed_at=p.observed_at, field="close", value=p.value,
                provider_id=ds.provider_id, pipeline_run_id=run_id, raw_id=raw_id,
            ))
            inserted += 1
        elif abs(cur.value - p.value) > 1e-9 * max(1.0, abs(cur.value)):
            # Market corrections overwrite in place; the prior value stays recoverable from raw storage.
            cur.value, cur.pipeline_run_id, cur.raw_id, cur.ingested_at = p.value, run_id, raw_id, utcnow()
            revised += 1
    return inserted, revised


def _publish_positioning(session: Session, ds: Dataset, points: list[ParsedPoint], run_id: str, raw_id: str) -> tuple[int, int]:
    if not points:
        return 0, 0
    existing = {
        o.report_date: o
        for o in session.execute(
            select(PositioningObservation).where(
                PositioningObservation.series_id == ds.dataset_id,
                PositioningObservation.report_date >= points[0].observed_at,
            )
        ).scalars()
    }
    inserted = revised = 0
    for p in points:
        fields = p.fields or {}
        cur = existing.get(p.observed_at)
        if cur is None:
            session.add(PositioningObservation(
                series_id=ds.dataset_id, report_date=p.observed_at, provider_id=ds.provider_id,
                pipeline_run_id=run_id, raw_id=raw_id, **fields,
            ))
            inserted += 1
        elif any(abs(getattr(cur, k) - v) > 1e-9 for k, v in fields.items()):
            for k, v in fields.items():
                setattr(cur, k, v)
            cur.pipeline_run_id, cur.raw_id, cur.ingested_at = run_id, raw_id, utcnow()
            revised += 1
    return inserted, revised


def _publish_trade(session: Session, ds: Dataset, points: list[ParsedPoint], run_id: str, raw_id: str) -> tuple[int, int]:
    if not points:
        return 0, 0
    existing = {
        (o.year, o.flow, o.cmd_code): o
        for o in session.execute(select(TradeFlow).where(TradeFlow.dataset_id == ds.dataset_id)).scalars()
    }
    inserted = revised = 0
    for p in points:
        dims = dict(p.dims or ())
        key = (p.observed_at, dims["flow"], dims["cmd"])
        cur = existing.get(key)
        if cur is None:
            session.add(TradeFlow(dataset_id=ds.dataset_id, year=p.observed_at, flow=dims["flow"], cmd_code=dims["cmd"],
                                  value_usd=p.value, provider_id=ds.provider_id, pipeline_run_id=run_id, raw_id=raw_id))
            inserted += 1
        elif abs(cur.value_usd - p.value) > 1e-6 * max(1.0, abs(cur.value_usd)):
            cur.value_usd, cur.pipeline_run_id, cur.raw_id, cur.ingested_at = p.value, run_id, raw_id, utcnow()
            revised += 1
    return inserted, revised


def _publish_macro(session: Session, ds: Dataset, points: list[ParsedPoint], run_id: str, raw_id: str) -> tuple[int, int]:
    """Point-in-time publish: a changed value is stored as a new vintage, never overwritten."""
    if not points:
        return 0, 0
    latest_vintage = (
        select(MacroObservation.period, func.max(MacroObservation.vintage_at).label("v"))
        .where(MacroObservation.series_id == ds.dataset_id, MacroObservation.period >= points[0].observed_at)
        .group_by(MacroObservation.period)
        .subquery()
    )
    latest = {
        o.period: o
        for o in session.execute(
            select(MacroObservation).join(
                latest_vintage,
                (MacroObservation.period == latest_vintage.c.period)
                & (MacroObservation.vintage_at == latest_vintage.c.v),
            ).where(MacroObservation.series_id == ds.dataset_id)
        ).scalars()
    }
    now = utcnow()
    inserted = revised = 0
    for p in points:
        cur = latest.get(p.observed_at)
        if cur is not None and abs(cur.value - p.value) <= 1e-9 * max(1.0, abs(cur.value)):
            continue
        session.add(MacroObservation(
            series_id=ds.dataset_id, period=p.observed_at, release_at=None, vintage_at=now,
            value=p.value, provider_id=ds.provider_id, pipeline_run_id=run_id, raw_id=raw_id,
        ))
        if cur is None:
            inserted += 1
        else:
            revised += 1
    return inserted, revised


def ingest_dataset(
    ds: Dataset,
    *,
    backfill: bool = False,
    adapter: ProviderAdapter | None = None,
    replay: FetchResult | None = None,
) -> DatasetResult:
    if not ds.has_source:
        return DatasetResult(ds.dataset_id, status="no_source", error="no provider configured")
    adapter = adapter or get_adapter(ds.provider_id)  # type: ignore[arg-type]

    with session_scope() as s:
        state = s.get(DatasetState, ds.dataset_id) or DatasetState(
            dataset_id=ds.dataset_id, kind=ds.kind, provider_id=ds.provider_id, consecutive_failures=0
        )
        s.merge(state)
        run = PipelineRun(pipeline_id=PIPELINE_ID, dataset_id=ds.dataset_id)
        s.add(run)
        s.flush()
        run_id = run.execution_id
        since = None if backfill or state.watermark is None else state.watermark - OVERLAP.get(ds.frequency, timedelta(days=400))

    result = DatasetResult(ds.dataset_id, status="running", execution_id=run_id)
    try:
        fetched = replay or adapter.fetch(ds.provider_symbol, since=since)  # type: ignore[arg-type]
        storage_uri, digest = _store_raw(ds.provider_id, ds.provider_symbol, fetched.payload, fetched.content_type)  # type: ignore[arg-type]
        points = adapter.parse(ds.provider_symbol, fetched.payload)  # type: ignore[arg-type]
        result.rows_fetched = len(points)

        with session_scope() as s:
            raw = RawPayload(
                provider_id=ds.provider_id, dataset_id=ds.dataset_id, request=fetched.request,
                storage_uri=storage_uri, sha256=digest, bytes=len(fetched.payload), pipeline_run_id=run_id,
            )
            s.add(raw)
            s.flush()

            asset_class = ds.meta.get("asset_class", "macro")
            clean, findings, metrics = quality.run_checks(
                points,
                frequency=ds.frequency,
                allow_non_positive=ds.kind in ("macro", "positioning") or asset_class in NON_POSITIVE_OK,
                # Level-ratio unit checks are meaningless for series that cross zero (net positions, growth rates).
                previous_values=None if ds.kind in ("positioning", "trade") or "percent" in ds.unit else _previous_values(s, ds),
            )
            result.findings = quality.to_json(findings)
            run_row = s.get(PipelineRun, run_id)
            assert run_row is not None
            run_row.rows_fetched = len(points)
            run_row.quality_metrics = {"metrics": metrics, "findings": result.findings, "since": since.isoformat() if since else None}

            state = s.get(DatasetState, ds.dataset_id)
            assert state is not None
            state.last_checked_at = utcnow()

            if quality.blocking(findings):
                run_row.status = result.status = "blocked"
                run_row.error = result.error = "; ".join(f.message for f in findings if f.severity == "error")
                state.last_error = result.error
                state.consecutive_failures = (state.consecutive_failures or 0) + 1
            else:
                publish = {"market": _publish_market, "macro": _publish_macro, "positioning": _publish_positioning,
                           "trade": _publish_trade}[ds.kind]
                ins, rev = publish(s, ds, clean, run_id, raw.raw_id)
                result.rows_inserted, result.rows_revised = ins, rev
                run_row.rows_inserted, run_row.rows_revised = ins, rev
                run_row.status = result.status = "success"
                if clean:
                    newest = max(p.observed_at for p in clean)
                    state.watermark = max(state.watermark or newest, newest)
                if ins or rev:
                    state.last_changed_at = utcnow()
                state.last_error = None
                state.consecutive_failures = 0
            run_row.finished_at = utcnow()
    except Exception as exc:  # provider outage, parse failure, etc.
        log.exception("ingestion failed for %s", ds.dataset_id)
        result.status, result.error = "failed", f"{type(exc).__name__}: {exc}"
        with session_scope() as s:
            run_row = s.get(PipelineRun, run_id)
            if run_row:
                run_row.status, run_row.error, run_row.finished_at = "failed", result.error, utcnow()
            state = s.get(DatasetState, ds.dataset_id)
            if state:
                state.last_checked_at = utcnow()
                state.last_error = result.error
                state.consecutive_failures = (state.consecutive_failures or 0) + 1
    return result


def ingest(dataset_ids: Iterable[str] | None = None, *, backfill: bool = False) -> list[DatasetResult]:
    all_ds = datasets()
    ids = list(dataset_ids) if dataset_ids else list(all_ds)
    results = []
    adapters: dict[str, ProviderAdapter] = {}
    for dsid in ids:
        ds = all_ds.get(dsid)
        if ds is None:
            results.append(DatasetResult(dsid, status="unknown_dataset", error="not in registry"))
            continue
        adapter = None
        if ds.has_source:
            adapter = adapters.setdefault(ds.provider_id, get_adapter(ds.provider_id))  # type: ignore[arg-type]
        res = ingest_dataset(ds, backfill=backfill, adapter=adapter)
        log.info("%s: %s fetched=%s inserted=%s revised=%s", dsid, res.status, res.rows_fetched, res.rows_inserted, res.rows_revised)
        results.append(res)

    # Step 07 — trigger intelligence: evaluate alert rules against freshly published data.
    from atlas.intelligence import evaluate_alerts

    changed = {r.dataset_id for r in results if r.rows_inserted or r.rows_revised}
    if changed:
        evaluate_alerts(changed)
    return results


def replay_raw(raw_id: str) -> DatasetResult:
    """Re-parse and re-publish a stored raw payload (idempotent)."""
    with session_scope() as s:
        raw = s.get(RawPayload, raw_id)
        if raw is None:
            raise KeyError(raw_id)
        ds = datasets()[raw.dataset_id]
        replay = FetchResult(
            request=f"replay:{raw_id}", payload=Path(raw.storage_uri).read_bytes(), content_type="text/csv"
        )
    return ingest_dataset(ds, backfill=True, replay=replay)


FAILURE_RETRY = timedelta(hours=1)
REFRESH_INTERVAL = {"daily_poll": timedelta(hours=6), "weekly_poll": timedelta(days=7), "none": None}


def due_datasets(now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    due = []
    with session_scope() as s:
        states = {st.dataset_id: st for st in s.execute(select(DatasetState)).scalars()}
    for ds in datasets().values():
        if not ds.has_source:
            continue
        interval = REFRESH_INTERVAL.get(ds.meta.get("refresh", "daily_poll"), timedelta(hours=6))
        if interval is None:
            continue
        st = states.get(ds.dataset_id)
        if st is not None and st.consecutive_failures:
            # Failed refreshes (outages, quota limits) retry sooner than the normal cadence, backing off per failure.
            interval = min(interval, FAILURE_RETRY * min(st.consecutive_failures, 6))
        if st is None or st.last_checked_at is None or now - st.last_checked_at >= interval:
            due.append(ds.dataset_id)
    return due
