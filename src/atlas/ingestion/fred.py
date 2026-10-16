"""FRED connector (Federal Reserve Bank of St. Louis).

Uses the public `fredgraph.csv` endpoint, which needs no API key. The `cosd` parameter
gives incremental extraction from a watermark.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"


class FredAdapter(ProviderAdapter):
    provider_id = "fred"
    display_name = "FRED, Federal Reserve Bank of St. Louis"
    homepage = "https://fred.stlouisfed.org"
    licence_note = (
        "Public data; some series are copyrighted by third parties (e.g. S&P, CBOE, ICE). "
        "Check series notes before redistribution."
    )

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=settings.http_timeout,
            headers={"User-Agent": "ATLAS-research-platform/0.1"},
            follow_redirects=True,
        )

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        params = {"id": symbol}
        if since:
            params["cosd"] = since.isoformat()
        resp = with_retries(lambda: self._client.get(BASE, params=params))
        if "text/html" in resp.headers.get("content-type", "") or resp.content.lstrip().startswith(b"<"):
            raise ProviderError(f"FRED returned HTML for {symbol}; series may not exist or was withdrawn")
        return FetchResult(request=str(resp.request.url), payload=resp.content, content_type="text/csv")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")))
        header = next(reader, None)
        if not header or len(header) < 2:
            raise ProviderError("empty or malformed CSV header")
        if header[0].lower() not in ("observation_date", "date") or header[1] != symbol:
            raise ProviderError(f"unexpected CSV header {header!r} for {symbol}")
        points: list[ParsedPoint] = []
        for row in reader:
            if not row:
                continue
            d = date.fromisoformat(row[0])
            raw = row[1].strip() if len(row) > 1 else ""
            value = None if raw in ("", ".") else float(raw)
            points.append(ParsedPoint(observed_at=d, value=value))
        return points

    def citation(self, symbol: str) -> str:
        return f"FRED series {symbol}, Federal Reserve Bank of St. Louis, {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        return f"https://fred.stlouisfed.org/series/{symbol}"
