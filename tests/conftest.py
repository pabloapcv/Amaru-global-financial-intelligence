"""Test fixtures: an isolated SQLite database seeded with synthetic data (no network)."""

from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("atlas")
    os.environ["ATLAS_DATABASE_URL"] = f"sqlite:///{root / 'test.db'}"
    os.environ["ATLAS_OBJECT_STORE"] = str(root / "objects")
    os.environ["ATLAS_LLM"] = "off"
    import importlib

    import atlas.config as config

    importlib.reload(config)
    import atlas.data.db as db

    db.settings = config.settings
    db._engine = None
    db.init_db(config.settings.database_url)
    for mod in ("atlas.ingestion.pipeline", "atlas.reporting", "atlas.ingestion.fred", "atlas.research.llm"):
        m = importlib.import_module(mod)
        if hasattr(m, "settings"):
            m.settings = config.settings
    with db.session_scope() as s:
        from atlas.instruments import sync_registry

        sync_registry(s)
    yield root


def make_csv(symbol: str, dates: pd.DatetimeIndex, values: np.ndarray) -> bytes:
    lines = [f"observation_date,{symbol}"] + [f"{d.date().isoformat()},{v:.6f}" for d, v in zip(dates, values)]
    return ("\n".join(lines) + "\n").encode()


class FakeFred:
    """Adapter stand-in serving deterministic synthetic series."""

    provider_id = "fred"
    display_name = "FRED (synthetic test data)"

    def __init__(self, overrides: dict[str, bytes] | None = None):
        self.overrides = overrides or {}
        self.calls: list[tuple[str, date | None]] = []

    def _cot(self, symbol: str) -> bytes:
        import json

        rng = np.random.default_rng(int(symbol))
        dates = pd.date_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=10), periods=400, freq="W-TUE")
        net = np.cumsum(rng.normal(0, 4000, len(dates)))
        rows = []
        for d, n in zip(dates, net):
            oi = 500000 + rng.normal(0, 10000)
            long_, short = 150000 + max(n, 0), 150000 + max(-n, 0)
            rows.append({
                "report_date_as_yyyy_mm_dd": f"{d.date().isoformat()}T00:00:00.000",
                "open_interest_all": str(int(oi)), "noncomm_positions_long_all": str(int(long_)),
                "noncomm_positions_short_all": str(int(short)), "noncomm_postions_spread_all": "50000",
                "comm_positions_long_all": str(int(short + 100000)), "comm_positions_short_all": str(int(long_ + 100000)),
                "nonrept_positions_long_all": "20000", "nonrept_positions_short_all": "20000",
            })
        return json.dumps(rows).encode()

    def _wb(self, symbol: str) -> bytes:
        import json

        iso3, code = symbol.split(":")
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        last_year = pd.Timestamp.today().year - 1
        rows = [{"date": str(y), "value": None if y < 1995 else round(float(rng.normal(3, 2)), 3)}
                for y in range(1980, last_year + 1)]
        return json.dumps({"country": iso3, "indicator": code, "rows": rows}).encode()

    def _series(self, symbol: str) -> bytes:
        if symbol in self.overrides:
            return self.overrides[symbol]
        if ":" in symbol:
            return self._wb(symbol)
        if symbol.isdigit():
            return self._cot(symbol)
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        monthly = symbol.startswith("P") or symbol in ("CPIAUCSL", "CPILFESL", "UNRATE", "PAYEMS", "INDPRO")
        end = pd.Timestamp.today().normalize()
        if monthly:
            dates = pd.date_range(end - pd.DateOffset(years=15), end - pd.DateOffset(months=2), freq="MS")
        elif symbol == "GDPC1":
            dates = pd.date_range(end - pd.DateOffset(years=20), end - pd.DateOffset(months=4), freq="QS")
        else:
            dates = pd.bdate_range(end - pd.DateOffset(years=8), end - pd.DateOffset(days=3))
        if symbol.startswith(("DGS", "DFII", "T10", "DFF", "UNRATE")):
            values = 3 + np.cumsum(rng.normal(0, 0.03, len(dates)))
        else:
            values = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01 if not monthly else 0.04, len(dates))))
        return make_csv(symbol, dates, values)

    def fetch(self, symbol, since=None):
        from atlas.provider_sdk import FetchResult

        self.calls.append((symbol, since))
        payload = self._series(symbol)
        if since and (":" in symbol or symbol.isdigit()):
            pass  # JSON fakes return full history; idempotent publish handles the overlap
        elif since:
            lines = payload.decode().splitlines()
            payload = ("\n".join([lines[0]] + [ln for ln in lines[1:] if ln[:10] >= since.isoformat()]) + "\n").encode()
        return FetchResult(request=f"fake://{symbol}", payload=payload, content_type="text/csv")

    def parse(self, symbol, payload):
        from atlas.ingestion.cftc import CftcAdapter
        from atlas.ingestion.fred import FredAdapter
        from atlas.ingestion.worldbank import WorldBankAdapter

        cls = WorldBankAdapter if ":" in symbol else CftcAdapter if symbol.isdigit() else FredAdapter
        return cls.parse(cls.__new__(cls), symbol, payload)

    def citation(self, symbol):
        return f"Synthetic {symbol}"

    def source_url(self, symbol):
        return f"https://example.invalid/{symbol}"


@pytest.fixture(scope="session")
def seeded(isolated_env):
    """Ingest synthetic data for every sourced dataset once per session."""
    from atlas.ingestion import pipeline
    from atlas.instruments import datasets

    fake = FakeFred()
    for ds in datasets().values():
        if ds.has_source:
            res = pipeline.ingest_dataset(ds, adapter=fake)  # type: ignore[arg-type]
            assert res.status == "success", (ds.dataset_id, res.error)
    return fake
