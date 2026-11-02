"""BIS statistics connector (public SDMX API, no key).

Symbols are "<dataset>.<BIS area code>":
- CBPOL: central bank policy rates, daily, because the daily series dates each decision, which the
  month-end series can't.
- EER: real effective exchange rate, broad basket, monthly (2020 = 100; up means a stronger currency).
- CREDITGAP: credit-to-GDP gap, quarterly: total credit to the private non-financial sector as a
  share of GDP minus its long-run trend, in percentage points (the Basel III early-warning measure).
- SPP: residential property prices, real (deflated by consumer prices), quarterly, 2010 = 100.
Only the observations are requested (detail=dataonly): the full format repeats long notes on
every row and is over ten times the size.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

BASE = "https://stats.bis.org/api/v2/data/dataflow/BIS/{flow}/1.0/{key}"
FIRST_YEAR = date(2000, 1, 1)
# dataset -> (BIS dataflow, series key, column holding the area, frequency label)
FLOWS = {
    "CBPOL": ("WS_CBPOL", "D.{area}", "REF_AREA", "daily"),
    "EER": ("WS_EER", "M.R.B.{area}", "REF_AREA", "monthly"),
    "CREDITGAP": ("WS_CREDIT_GAP", "Q.{area}.P.A.C", "BORROWERS_CTY", "quarterly"),
    "SPP": ("WS_SPP", "Q.{area}.R.628", "REF_AREA", "quarterly"),
}


def _flow(symbol: str) -> tuple[str, str, str, str, str]:
    dataset, _, area = symbol.partition(".")
    if dataset not in FLOWS or not area:
        raise ProviderError(f"unknown BIS symbol {symbol}")
    flow, key, column, freq = FLOWS[dataset]
    return flow, key.format(area=area), column, freq, area


def _period(text: str) -> date:
    """BIS periods: 2026-10-23 (daily), 2026-09 (monthly) or 2026-Q1 (quarterly, dated to its first day)."""
    if "-Q" in text:
        year, q = text.split("-Q")
        return date(int(year), 3 * int(q) - 2, 1)
    if len(text) == 7:
        return date(int(text[:4]), int(text[5:]), 1)
    return date.fromisoformat(text)


class BisAdapter(ProviderAdapter):
    provider_id = "bis"
    display_name = "Bank for International Settlements statistics"
    homepage = "https://data.bis.org"
    licence_note = "Cite the BIS as the source; see the BIS terms for statistics."

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent}
        )

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        flow, key, _, freq, _ = _flow(symbol)
        start = since or FIRST_YEAR
        period = start.isoformat() if freq == "daily" else f"{start.year}-{start.month:02d}" if freq == "monthly" else f"{start.year}-Q{(start.month - 1) // 3 + 1}"
        url = BASE.format(flow=flow, key=key)
        params = {"startPeriod": period, "detail": "dataonly", "format": "csv"}
        resp = with_retries(lambda: self._client.get(url, params=params, headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"}))
        return FetchResult(request=str(resp.request.url), payload=resp.content, content_type="text/csv")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        flow, _, column, _, area = _flow(symbol)
        rows = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
        if not {column, "TIME_PERIOD", "OBS_VALUE"} <= set(rows.fieldnames or ()):
            raise ProviderError(f"unexpected BIS payload for {symbol}: {payload[:200]!r}")
        out = []
        for r in rows:
            if r[column] != area:
                raise ProviderError(f"payload has area {r[column]}, expected {area}")
            if not r.get("DATAFLOW", flow).startswith(f"BIS:{flow}"):
                raise ProviderError(f"payload is {r['DATAFLOW']}, expected {flow}")
            out.append(ParsedPoint(_period(r["TIME_PERIOD"]), float(r["OBS_VALUE"]) if r["OBS_VALUE"] else None))
        return out

    def citation(self, symbol: str) -> str:
        flow, _, _, freq, area = _flow(symbol)
        names = {"WS_CBPOL": "central bank policy rates", "WS_EER": "real effective exchange rates, broad",
                 "WS_CREDIT_GAP": "credit-to-GDP gaps", "WS_SPP": "residential property prices, real"}
        return f"BIS {names[flow]} ({flow}), {area}, {freq}, {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        flow, key, _, _, _ = _flow(symbol)
        topic = {"WS_CBPOL": "CBPOL", "WS_EER": "EER", "WS_CREDIT_GAP": "CREDIT_GAP", "WS_SPP": "RPP"}[flow]
        return f"https://data.bis.org/topics/{topic}/BIS,{flow},1.0/{key}"
