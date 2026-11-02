"""U.S. Energy Information Administration connector (API v2, weekly petroleum and natural gas data).

Symbols are EIA series ids such as "PET.WCESTUS1.W". The key goes in a header so it never appears in
a stored request URL or a log line. Without EIA_API_KEY the public DEMO_KEY is used, which allows
about ten calls an hour: enough for a daily refresh of a few weekly series, not for more.
Each series is fetched whole (a weekly series since 1982 is ~2,300 rows), and a changed unit fails.
"""

from __future__ import annotations

import json
from datetime import date

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

BASE = "https://api.eia.gov/v2/seriesid/{symbol}"
DEMO_KEY = "DEMO_KEY"
UNITS = {"WCESTUS1": "MBBL", "WCSSTUS1": "MBBL", "WGTSTUS1": "MBBL", "WDISTUS1": "MBBL", "WCRFPUS2": "MBBL/D",
         "WCRIMUS2": "MBBL/D", "WPULEUS3": "%", "NW2_EPG0_SWO_R48_BCF": "BCF"}
MAX_ROWS = 5000


class EiaAdapter(ProviderAdapter):
    provider_id = "eia"
    display_name = "U.S. Energy Information Administration"
    homepage = "https://www.eia.gov/opendata/"
    licence_note = "U.S. government data, public domain; EIA asks to be cited as the source."

    def __init__(self, client: httpx.Client | None = None, api_key: str | None = None):
        self._client = client or httpx.Client(timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent})
        self._key = api_key or settings.eia_api_key or DEMO_KEY

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        params = {"length": MAX_ROWS, "sort[0][column]": "period", "sort[0][direction]": "asc"}
        try:
            resp = with_retries(lambda: self._client.get(BASE.format(symbol=symbol), params=params, headers={"X-Api-Key": self._key}))
        except ProviderError as exc:
            if "429" in str(exc) and self._key == DEMO_KEY:
                raise ProviderError("EIA rate limit reached with the public demo key; set EIA_API_KEY (free at eia.gov/opendata)") from exc
            raise
        return FetchResult(request=str(resp.request.url), payload=resp.content, content_type="application/json")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        try:
            body = json.loads(payload)
            data, total = body["response"]["data"], int(body["response"]["total"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(f"unexpected EIA payload for {symbol}: {payload[:200]!r}") from exc
        if total > len(data):
            raise ProviderError(f"EIA returned {len(data)} of {total} rows for {symbol}")
        code = symbol.split(".")[1]
        out = []
        for r in data:
            if r.get("series") != code:
                raise ProviderError(f"payload has series {r.get('series')}, expected {code}")
            if code in UNITS and r.get("units") != UNITS[code]:
                raise ProviderError(f"EIA unit for {symbol} changed from {UNITS[code]} to {r.get('units')}")
            value = r.get("value")
            out.append(ParsedPoint(date.fromisoformat(r["period"]), None if value in (None, "") else float(value)))
        return sorted(out, key=lambda p: p.observed_at)

    def citation(self, symbol: str) -> str:
        return f"U.S. Energy Information Administration, series {symbol}, weekly, {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        area = "ng" if symbol.startswith("NG.") else "pet"
        return f"https://www.eia.gov/dnav/{area}/hist/LeafHandler.ashx?n={symbol.split('.')[0]}&s={symbol.split('.')[1]}&f=W"
