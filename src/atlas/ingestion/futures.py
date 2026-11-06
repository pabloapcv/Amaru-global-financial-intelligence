"""Thin futures data adapter using free delayed sources.

Sources:
- Stooq: Free delayed continuous futures for many contracts (https://stooq.com)
- Yahoo Finance: Free delayed data for some futures proxies

This adapter provides delayed (typically 15-20 minute) continuous contract data
suitable for research and backtesting, NOT for live trading.

Quality notes:
- Continuous contracts are synthetic (rolled at expiration)
- Data quality varies by source and contract
- Gaps and spikes can occur around rolls
- This is free delayed data with no SLA

To use real exchange data with proper roll methodology, implement a licensed
adapter (ICE, CME, etc.) using the same ProviderAdapter interface.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

STOOQ_SYMBOLS = {
    "FUT.GC": "gc.f",
    "FUT.CL": "cl.f",
    "FUT.SI": "si.f",
    "FUT.NG": "ng.f",
    "FUT.ZC": "zc.f",
    "FUT.ZW": "zw.f",
    "FUT.ZS": "zs.f",
    "FUT.ES": "es.f",
}

STOOQ_NAMES = {
    "FUT.GC": "Gold Continuous (Stooq/COMEX)",
    "FUT.CL": "Crude Oil WTI Continuous (Stooq/NYMEX)",
    "FUT.SI": "Silver Continuous (Stooq/COMEX)",
    "FUT.NG": "Natural Gas Continuous (Stooq/NYMEX)",
    "FUT.ZC": "Corn Continuous (Stooq/CBOT)",
    "FUT.ZW": "Wheat Continuous (Stooq/CBOT)",
    "FUT.ZS": "Soybeans Continuous (Stooq/CBOT)",
    "FUT.ES": "E-mini S&P 500 Continuous (Stooq/CME)",
}


class FuturesAdapter(ProviderAdapter):
    """Adapter for free delayed futures data from Stooq.

    Stooq provides free historical and delayed quotes for continuous futures
    contracts. Data is typically delayed 15-20 minutes.

    Usage:
        adapter = FuturesAdapter()
        result = adapter.fetch("FUT.GC")  # Gold continuous
        points = adapter.parse("FUT.GC", result.payload)
    """

    provider_id = "futures_free"
    display_name = "Free Delayed Futures (Stooq)"
    homepage = "https://stooq.com"
    licence_note = (
        "Stooq provides free delayed data for personal/research use. "
        "No SLA; not suitable for commercial redistribution or live trading. "
        "See https://stooq.com for terms."
    )

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def _stooq_url(self, symbol: str, start: date | None) -> str:
        stooq_sym = STOOQ_SYMBOLS.get(symbol)
        if not stooq_sym:
            raise ProviderError(f"Unknown futures symbol: {symbol}. Available: {list(STOOQ_SYMBOLS.keys())}")
        base = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
        if start:
            base += f"&d1={start.strftime('%Y%m%d')}"
        return base

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        url = self._stooq_url(symbol, since)
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            resp = with_retries(lambda: client.get(url, headers={"User-Agent": "Amaru-research/0.1"}))
        if not resp.content or b"No data" in resp.content:
            raise ProviderError(f"No data returned from Stooq for {symbol}")
        return FetchResult(request=url, payload=resp.content, content_type="text/csv")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        text = payload.decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
        if len(lines) < 2:
            return []

        header = lines[0].lower().split(",")
        try:
            date_idx = header.index("date")
            close_idx = header.index("close")
        except ValueError:
            raise ProviderError(f"Unexpected Stooq CSV format: {header}")

        points = []
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= max(date_idx, close_idx):
                continue
            try:
                d = date.fromisoformat(parts[date_idx])
                v = float(parts[close_idx])
                points.append(ParsedPoint(observed_at=d, value=v))
            except (ValueError, IndexError):
                continue

        return sorted(points, key=lambda p: p.observed_at)

    def citation(self, symbol: str) -> str:
        name = STOOQ_NAMES.get(symbol, symbol)
        return f"{name}, delayed continuous contract via Stooq"

    def source_url(self, symbol: str) -> str:
        stooq_sym = STOOQ_SYMBOLS.get(symbol, symbol.lower())
        return f"https://stooq.com/q/?s={stooq_sym}"


class FixtureFuturesAdapter(ProviderAdapter):
    """Fixture adapter for testing futures without network.

    Generates synthetic but realistic futures price history.
    """

    provider_id = "futures_fixture"
    display_name = "Fixture Futures (synthetic)"
    homepage = "N/A"
    licence_note = "Synthetic test data, not real prices."

    def __init__(self, seed: int = 12345):
        import numpy as np
        self.rng = np.random.default_rng(seed)
        self._cache: dict[str, bytes] = {}

    def _generate(self, symbol: str) -> bytes:
        import numpy as np
        import pandas as pd

        base_prices = {
            "FUT.GC": 1800,
            "FUT.CL": 75,
            "FUT.SI": 24,
            "FUT.NG": 3.5,
            "FUT.ZC": 450,
            "FUT.ZW": 650,
            "FUT.ZS": 1350,
            "FUT.ES": 4500,
        }
        base = base_prices.get(symbol, 100)
        vol = 0.25 if symbol in ("FUT.NG", "FUT.CL") else 0.15

        end = pd.Timestamp.today().normalize() - pd.Timedelta(days=1)
        dates = pd.bdate_range(end - pd.DateOffset(years=8), end)
        returns = self.rng.normal(0.0001, vol / 16, len(dates))
        prices = base * np.exp(np.cumsum(returns))

        lines = ["Date,Open,High,Low,Close,Volume"]
        for d, p in zip(dates, prices):
            lines.append(f"{d.date().isoformat()},{p:.2f},{p * 1.01:.2f},{p * 0.99:.2f},{p:.2f},1000000")
        return "\n".join(lines).encode()

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        if symbol not in self._cache:
            self._cache[symbol] = self._generate(symbol)
        return FetchResult(request=f"fixture://{symbol}", payload=self._cache[symbol], content_type="text/csv")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        return FuturesAdapter().parse(symbol, payload)

    def citation(self, symbol: str) -> str:
        return f"Synthetic fixture data for {symbol} (not real prices)"

    def source_url(self, symbol: str) -> str:
        return "N/A"
