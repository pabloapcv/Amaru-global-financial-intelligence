"""Test fixtures: an isolated database seeded with synthetic data (no network).

SQLite by default; set ATLAS_TEST_DATABASE_URL (e.g. a throwaway PostgreSQL/TimescaleDB) to run
the same suite against Postgres. That database is wiped at the start of the session.
"""

from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(scope="session", autouse=True)
def no_network():
    """Tests never reach the internet: real HTTP transports fail loudly (mock transports still work)."""
    import httpx

    def blocked(self, request):
        raise RuntimeError(f"network access in tests: {request.method} {request.url}")

    original = httpx.HTTPTransport.handle_request
    httpx.HTTPTransport.handle_request = blocked
    yield
    httpx.HTTPTransport.handle_request = original


@pytest.fixture(scope="session", autouse=True)
def isolated_env(tmp_path_factory):
    root = tmp_path_factory.mktemp("atlas")
    test_db = os.getenv("ATLAS_TEST_DATABASE_URL")
    os.environ["ATLAS_DATABASE_URL"] = test_db or f"sqlite:///{root / 'test.db'}"
    os.environ["ATLAS_OBJECT_STORE"] = str(root / "objects")
    os.environ["ATLAS_LLM"] = "off"
    os.environ["ATLAS_AUTH"] = "off"  # auth tests opt in explicitly
    os.environ["ATLAS_SECRET_KEY"] = "test-only-secret-key"  # never create a key file in the real data/
    import importlib

    import atlas.config as config

    importlib.reload(config)
    import atlas.data.db as db

    db.settings = config.settings
    db._engine = None
    if test_db:
        db.Base.metadata.drop_all(db.get_engine(config.settings.database_url))
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


def _is_trade(symbol: str) -> bool:
    parts = symbol.split(":")
    return len(parts) == 2 and len(parts[1]) == 3 and parts[1].isalpha()


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

    def _trade(self, symbol: str) -> bytes:
        import json

        reporter, partner = symbol.split(":")
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        last = pd.Timestamp.today().year - 1
        rows = []
        for y in range(last - 7, last + 1):
            for flow in ("X", "M"):
                rows.append({"year": y, "flow": flow, "cmd": "TOTAL", "value": float(rng.uniform(1e8, 1e9))})
        for flow in ("X", "M"):
            for ch in ("26", "71", "08", "84", "27"):
                rows.append({"year": last, "flow": flow, "cmd": ch, "value": float(rng.uniform(1e6, 5e7))})
        return json.dumps({"reporter": reporter, "partner": partner, "rows": rows}).encode()

    def _psd(self, symbol: str) -> bytes:
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        last = pd.Timestamp.today().year
        lines = ["market_year,country_code,country_name,attribute,value,release"]
        for code, name, scale in (("BR", "Brazil", 40000), ("IN", "India", 30000), ("US", "United States", 20000), ("TH", "Thailand", 10000)):
            for y in range(last - 24, last + 1):
                prod = scale * (1 + 0.02 * (y - last + 24)) * float(rng.uniform(0.9, 1.1))
                cons = prod * float(rng.uniform(0.7, 1.05))
                vals = {"production": prod, "consumption": cons, "exports": prod * 0.4, "imports": prod * 0.1,
                        "ending_stocks": cons * float(rng.uniform(0.15, 0.35)), "beginning_stocks": cons * 0.2}
                for attr, v in vals.items():
                    lines.append(f"{y},{code},{name},{attr},{v:.1f},{last}-0{min(9, 1 + y % 9)}")
        return ("\n".join(lines) + "\n").encode()

    def _policy_rate(self, symbol: str) -> bytes:
        area = symbol.split(".")[1]
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        dates = pd.date_range(pd.Timestamp.today().normalize() - pd.DateOffset(years=5), pd.Timestamp.today().normalize() - pd.Timedelta(days=5))
        steps = rng.choice([-0.25, 0.0, 0.25], len(dates), p=[0.01, 0.98, 0.01])
        lines = ["DATAFLOW,FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE"] + [f"BIS:WS_CBPOL(1.0),D,{area},{d.date().isoformat()},{v:.2f}" for d, v in zip(dates, 4 + np.cumsum(steps))]
        return ("\n".join(lines) + "\n").encode()

    def _pink_sheet(self, symbol: str) -> bytes:
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        end = pd.Timestamp.today().normalize()
        dates = pd.date_range(end - pd.DateOffset(years=15), end - pd.DateOffset(months=1), freq="MS")
        values = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.04, len(dates))))
        return ("\n".join(["month,value"] + [f"{d.date().isoformat()},{v:.3f}" for d, v in zip(dates, values)]) + "\n").encode()

    def _eia(self, symbol: str) -> bytes:
        import json

        from atlas.ingestion.eia import UNITS

        code = symbol.split(".")[1]
        rng = np.random.default_rng(abs(hash(symbol)) % 2**32)
        dates = pd.date_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=8), periods=520, freq="W-FRI")
        season = np.sin(2 * np.pi * dates.dayofyear.to_numpy() / 365.25)
        level = {"MBBL": 400000.0, "MBBL/D": 10000.0, "%": 90.0, "BCF": 3000.0}[UNITS[code]]
        values = level * (1 + 0.08 * season + np.cumsum(rng.normal(0, 0.004, len(dates))))
        rows = [{"period": d.date().isoformat(), "series": code, "units": UNITS[code], "value": round(float(v), 1)} for d, v in zip(dates, values)]
        return json.dumps({"response": {"total": len(rows), "data": rows}}).encode()

    def _series(self, symbol: str) -> bytes:
        if symbol in self.overrides:
            return self.overrides[symbol]
        if symbol.startswith(("PET.", "NG.")):
            return self._eia(symbol)
        if symbol.startswith("CMO."):
            return self._pink_sheet(symbol)
        if symbol.startswith("CBPOL."):
            return self._policy_rate(symbol)
        if symbol.startswith("psd_"):
            return self._psd(symbol)
        if _is_trade(symbol):
            return self._trade(symbol)
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
        if since and (":" in symbol or symbol.isdigit() or symbol.startswith(("psd_", "CBPOL.", "PET.", "NG."))):
            pass  # JSON fakes return full history; idempotent publish handles the overlap
        elif since:
            lines = payload.decode().splitlines()
            payload = ("\n".join([lines[0]] + [ln for ln in lines[1:] if ln[:10] >= since.isoformat()]) + "\n").encode()
        return FetchResult(request=f"fake://{symbol}", payload=payload, content_type="text/csv")

    def parse(self, symbol, payload):
        from atlas.ingestion.bis import BisAdapter
        from atlas.ingestion.cftc import CftcAdapter
        from atlas.ingestion.eia import EiaAdapter
        from atlas.ingestion.fred import FredAdapter
        from atlas.ingestion.pinksheet import PinkSheetAdapter
        from atlas.ingestion.worldbank import WorldBankAdapter

        from atlas.ingestion.comtrade import ComtradeAdapter

        from atlas.ingestion.psd import PsdAdapter

        cls = (EiaAdapter if symbol.startswith(("PET.", "NG.")) else BisAdapter if symbol.startswith("CBPOL.") else PinkSheetAdapter if symbol.startswith("CMO.") else PsdAdapter if symbol.startswith("psd_") else ComtradeAdapter if _is_trade(symbol)
               else WorldBankAdapter if ":" in symbol else CftcAdapter if symbol.isdigit() else FredAdapter)
        return cls.parse(cls.__new__(cls), symbol, payload)

    def citation(self, symbol):
        return f"Synthetic {symbol}"

    def source_url(self, symbol):
        return f"https://example.invalid/{symbol}"


@pytest.fixture
def no_source(monkeypatch):
    """A registered instrument with no price source: commercial mode switches off restricted data."""
    from atlas import instruments

    monkeypatch.setenv("ATLAS_COMMERCIAL_MODE", "true")
    instruments.datasets.cache_clear()
    yield "EQ.US.SP500"
    monkeypatch.delenv("ATLAS_COMMERCIAL_MODE")
    instruments.datasets.cache_clear()


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
