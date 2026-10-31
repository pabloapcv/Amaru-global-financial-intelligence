"""BIS central bank policy rates connector (public SDMX API, no key).

Symbols are "CBPOL.<BIS area code>". The daily series is used because it dates each decision,
which the month-end series can't. Only the observations are requested (detail=dataonly): the full
format repeats long notes on every row and is over ten times the size.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

BASE = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.{area}"
FIRST_YEAR = date(2000, 1, 1)


class BisAdapter(ProviderAdapter):
    provider_id = "bis"
    display_name = "Bank for International Settlements, central bank policy rates"
    homepage = "https://data.bis.org/topics/CBPOL"
    licence_note = "Cite the BIS as the source; see the BIS terms for statistics."

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent}
        )

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        url = BASE.format(area=symbol.split(".", 1)[1])
        params = {"startPeriod": (since or FIRST_YEAR).isoformat(), "detail": "dataonly", "format": "csv"}
        resp = with_retries(lambda: self._client.get(url, params=params, headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}))
        return FetchResult(request=str(resp.request.url), payload=resp.content, content_type="text/csv")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        area = symbol.split(".", 1)[1]
        rows = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        if not {"REF_AREA", "TIME_PERIOD", "OBS_VALUE"} <= set(rows.fieldnames or ()):
            raise ProviderError(f"unexpected BIS payload for {symbol}: {payload[:200]!r}")
        out = []
        for r in rows:
            if r["REF_AREA"] != area:
                raise ProviderError(f"payload has area {r['REF_AREA']}, expected {area}")
            out.append(ParsedPoint(date.fromisoformat(r["TIME_PERIOD"]), float(r["OBS_VALUE"]) if r["OBS_VALUE"] else None))
        return out

    def citation(self, symbol: str) -> str:
        return f"BIS central bank policy rates (WS_CBPOL), {symbol.split('.', 1)[1]}, daily, {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        return f"https://data.bis.org/topics/CBPOL/BIS,WS_CBPOL,1.0/D.{symbol.split('.', 1)[1]}"
