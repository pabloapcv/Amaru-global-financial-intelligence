"""CFTC Commitments of Traders connector (legacy report, futures only).

Public Socrata API, no key required. Each weekly record is published with the net
non-commercial (speculative) position as the primary value and the full position
breakdown in `fields`.
"""

from __future__ import annotations

import json
from datetime import date

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

BASE = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
FIELDS = {
    "open_interest": "open_interest_all",
    "noncomm_long": "noncomm_positions_long_all",
    "noncomm_short": "noncomm_positions_short_all",
    "noncomm_spread": "noncomm_postions_spread_all",  # sic: CFTC column name
    "comm_long": "comm_positions_long_all",
    "comm_short": "comm_positions_short_all",
    "nonrept_long": "nonrept_positions_long_all",
    "nonrept_short": "nonrept_positions_short_all",
}


class CftcAdapter(ProviderAdapter):
    provider_id = "cftc"
    display_name = "CFTC Commitments of Traders"
    homepage = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"
    licence_note = "U.S. government public data."

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent}
        )

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        where = f"cftc_contract_market_code='{symbol}'"
        if since:
            where += f" AND report_date_as_yyyy_mm_dd >= '{since.isoformat()}T00:00:00'"
        params = {
            "$select": ",".join(["report_date_as_yyyy_mm_dd", "market_and_exchange_names", *FIELDS.values()]),
            "$where": where,
            "$order": "report_date_as_yyyy_mm_dd ASC",
            "$limit": "50000",
        }
        resp = with_retries(lambda: self._client.get(BASE, params=params))
        return FetchResult(request=str(resp.request.url), payload=resp.content, content_type="application/json")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        try:
            rows = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"CFTC returned invalid JSON: {exc}") from exc
        if not isinstance(rows, list):
            raise ProviderError(f"unexpected CFTC payload: {str(rows)[:200]}")
        points = []
        for r in rows:
            try:
                fields = {k: float(r[col]) for k, col in FIELDS.items()}
            except (KeyError, TypeError, ValueError):
                continue  # incomplete record; counted as missing by quality checks
            net = fields["noncomm_long"] - fields["noncomm_short"]
            points.append(ParsedPoint(date.fromisoformat(r["report_date_as_yyyy_mm_dd"][:10]), net, fields))
        return points

    def citation(self, symbol: str) -> str:
        return f"CFTC Commitments of Traders, legacy futures-only report, contract market code {symbol}, {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        return "https://publicreporting.cftc.gov/stories/s/r4w3-av2u"
