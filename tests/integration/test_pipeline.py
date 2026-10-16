import pytest
from sqlalchemy import func, select

from atlas.data.access import get_series, series_meta
from atlas.data.db import MacroObservation, MarketObservation, RawPayload, session_scope, utcnow
from atlas.ingestion import pipeline
from atlas.instruments import get_dataset

from tests.conftest import FakeFred, make_csv


def _count(model, col, dsid):
    with session_scope() as s:
        return s.execute(select(func.count()).select_from(model).where(col == dsid)).scalar()


def test_ingestion_is_idempotent(seeded):
    ds = get_dataset("CMD.CRUDE.WTI")
    before = _count(MarketObservation, MarketObservation.instrument_id, ds.dataset_id)
    res = pipeline.ingest_dataset(ds, adapter=seeded)
    assert res.status == "success"
    assert res.rows_inserted == 0 and res.rows_revised == 0
    assert _count(MarketObservation, MarketObservation.instrument_id, ds.dataset_id) == before


def test_incremental_fetch_uses_watermark(seeded):
    ds = get_dataset("FX.USDJPY")
    pipeline.ingest_dataset(ds, adapter=seeded)
    symbol, since = seeded.calls[-1]
    assert symbol == "DEXJPUS" and since is not None


def test_macro_revision_creates_vintage_and_point_in_time_view(seeded):
    ds = get_dataset("US.UNRATE")
    original, _ = get_series(ds.dataset_id)
    cutoff = utcnow()
    revised = original.copy()
    revised.iloc[-1] = revised.iloc[-1] + 0.3
    fake = FakeFred({"UNRATE": make_csv("UNRATE", revised.index, revised.to_numpy())})
    res = pipeline.ingest_dataset(ds, adapter=fake, backfill=True)
    assert res.rows_revised == 1 and res.rows_inserted == 0

    latest, _ = get_series(ds.dataset_id)
    as_of, _ = get_series(ds.dataset_id, as_of=cutoff)
    assert latest.iloc[-1] == pytest.approx(revised.iloc[-1], abs=1e-6)
    assert as_of.iloc[-1] == pytest.approx(original.iloc[-1], abs=1e-9)
    with session_scope() as s:
        n = s.execute(select(func.count()).select_from(MacroObservation).where(
            MacroObservation.series_id == ds.dataset_id, MacroObservation.period == original.index[-1].date())).scalar()
    assert n == 2


def test_unit_change_is_blocked_and_reported(seeded):
    ds = get_dataset("CMD.NATGAS.HH")
    series, _ = get_series(ds.dataset_id)
    tail = series.iloc[-60:] * 1000  # e.g. provider switched units
    fake = FakeFred({"DHHNGSP": make_csv("DHHNGSP", tail.index, tail.to_numpy())})
    before = _count(MarketObservation, MarketObservation.instrument_id, ds.dataset_id)
    res = pipeline.ingest_dataset(ds, adapter=fake, backfill=True)
    assert res.status == "blocked"
    assert _count(MarketObservation, MarketObservation.instrument_id, ds.dataset_id) == before
    assert series_meta(ds.dataset_id).freshness == "failing"
    pipeline.ingest_dataset(ds, adapter=seeded)  # recover for other tests
    assert series_meta(ds.dataset_id).freshness != "failing"


def test_provider_outage_is_recorded(seeded):
    class Down(FakeFred):
        def fetch(self, symbol, since=None):
            raise ConnectionError("provider down")

    ds = get_dataset("FX.EURUSD")
    res = pipeline.ingest_dataset(ds, adapter=Down())
    assert res.status == "failed" and "provider down" in res.error
    pipeline.ingest_dataset(ds, adapter=seeded)


def test_raw_payload_replay_is_idempotent(seeded):
    with session_scope() as s:
        raw = s.execute(select(RawPayload).where(RawPayload.dataset_id == "CMD.COPPER.LME")).scalars().first()
        raw_id = raw.raw_id
    import atlas.ingestion.pipeline as p

    original = p.get_adapter
    p.get_adapter = lambda _pid: seeded
    try:
        res = pipeline.replay_raw(raw_id)
    finally:
        p.get_adapter = original
    assert res.status == "success" and res.rows_inserted == 0


def test_no_source_dataset_is_disclosed(seeded):
    res = pipeline.ingest_dataset(get_dataset("CMD.GOLD"))
    assert res.status == "no_source"
    assert series_meta("CMD.GOLD").freshness == "no_source"


def test_failed_datasets_retry_before_normal_cadence(seeded):
    from datetime import timedelta

    class Down(FakeFred):
        def fetch(self, symbol, since=None):
            raise ConnectionError("quota exceeded")

    ds = get_dataset("TRADE.BOL.USA")  # weekly cadence
    pipeline.ingest_dataset(ds, adapter=Down())
    soon = utcnow() + timedelta(hours=2)
    assert "TRADE.BOL.USA" in pipeline.due_datasets(soon)
    pipeline.ingest_dataset(ds, adapter=seeded)  # recover
    assert "TRADE.BOL.USA" not in pipeline.due_datasets(soon)
