"""Dataset export (FR-018): any stored dataset as CSV or JSON with its provenance.

CSV files start with `#` comment lines (dataset, source, citation, observation and ingestion
dates, freshness, licence note, point-in-time cut-off) unless `header=False`. Values are exactly
what Amaru stores; nothing is filled, interpolated or adjusted.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any

import pandas as pd

from atlas.data.access import get_fundamentals, get_positioning, get_series, get_trade, series_meta
from atlas.instruments import get_dataset

MAX_ROWS = 500_000


def _frame(dataset_id: str, start: date | None, end: date | None, as_of: datetime | None) -> pd.DataFrame:
    ds = get_dataset(dataset_id)
    if ds is None:
        raise KeyError(dataset_id)
    if ds.kind == "positioning":
        df, _ = get_positioning(dataset_id, start=start)
        df = df.reset_index().rename(columns={"date": "report_date"})
        if end:
            df = df[df["report_date"].dt.date <= end]
        df["report_date"] = df["report_date"].dt.strftime("%Y-%m-%d")
        return df
    if ds.kind == "trade":
        df, _ = get_trade(dataset_id)
        df = df.rename(columns={"flow": "flow_reporter_declared"})
        if start:
            df = df[df["year"] >= start.year]
        return df[df["year"] <= end.year] if end else df
    if ds.kind == "fundamentals":
        df, _ = get_fundamentals(dataset_id)
        df = df.rename(columns={"release": "usda_report", "prev_release": "previous_usda_report"})
        for col in ("usda_report", "previous_usda_report"):
            df[col] = df[col].map(lambda v: f"{int(v) // 100}-{int(v) % 100:02d}" if pd.notna(v) else None)
        if start:
            df = df[df["market_year"] >= start.year]
        if end:
            df = df[df["market_year"] <= end.year]
        return df.sort_values(["market_year", "country_name", "attribute"])
    s, _ = get_series(dataset_id, start=start, end=end, as_of=as_of)
    return pd.DataFrame({"date": s.index.strftime("%Y-%m-%d"), "value": s.values})


def provenance(dataset_id: str, as_of: datetime | None = None) -> dict[str, Any]:
    meta = series_meta(dataset_id)
    lic = meta.licence or {}
    return {
        "dataset_id": dataset_id, "name": meta.name, "kind": meta.kind, "unit": meta.unit, "frequency": meta.frequency,
        "source": meta.provider, "source_symbol": meta.provider_symbol, "source_url": meta.source_url, "citation": meta.citation,
        "observations_through": meta.last_observation, "ingested_at": meta.last_ingested_at, "freshness": meta.freshness,
        "licence": f"{lic.get('label')}: {lic.get('owner')}. {lic.get('note')}" if lic else None,
        "as_of": as_of.isoformat() if as_of else None,
        "price_basis": meta.price_basis,
    }


def export(dataset_id: str, fmt: str = "csv", *, start: date | None = None, end: date | None = None,
           as_of: datetime | None = None, header: bool = True) -> tuple[str, str]:
    """Returns (body, media type)."""
    df = _frame(dataset_id, start, end, as_of)
    if len(df) > MAX_ROWS:
        raise ValueError(f"export has {len(df)} rows; narrow it with start/end (max {MAX_ROWS})")
    prov = provenance(dataset_id, as_of)
    if fmt == "json":
        import json

        rows = json.loads(df.to_json(orient="records", date_format="iso"))
        return json.dumps({"provenance": prov, "columns": list(df.columns), "rows": rows}), "application/json"
    out = io.StringIO()
    if header:
        for k, v in prov.items():
            if v is not None:
                out.write(f"# {k}: {str(v).replace(chr(10), ' ')}\n")
    df.to_csv(out, index=False, quoting=csv.QUOTE_MINIMAL)
    return out.getvalue(), "text/csv; charset=utf-8"
