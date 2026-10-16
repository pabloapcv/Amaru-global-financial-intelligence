"""World Bank World Development Indicators connector (public API, no key).

Symbols are "<ISO3>:<indicator code>". The API returns many countries per request, so the
adapter fetches each indicator once per instance and serves every country from that response.
The stored raw payload is the country's own slice, which keeps replay per-dataset.
"""

from __future__ import annotations

import json
from datetime import date

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

BASE = "https://api.worldbank.org/v2/country/{countries}/indicator/{indicator}"


class WorldBankAdapter(ProviderAdapter):
    provider_id = "worldbank"
    display_name = "World Bank, World Development Indicators"
    homepage = "https://data.worldbank.org"
    licence_note = "CC BY 4.0."

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=settings.http_timeout, headers={"User-Agent": "ATLAS-research-platform/0.1"}
        )
        self._cache: dict[str, tuple[str, list[dict]]] = {}

    def _indicator(self, code: str) -> tuple[str, list[dict]]:
        if code not in self._cache:
            from atlas.instruments import countries

            iso = ";".join(c["iso3"] for c in countries())
            url = BASE.format(countries=iso, indicator=code)
            resp = with_retries(lambda: self._client.get(url, params={"format": "json", "per_page": "20000", "date": "1980:2030"}))
            body = resp.json()
            if not isinstance(body, list) or len(body) < 2 or not isinstance(body[1], list):
                raise ProviderError(f"unexpected World Bank payload for {code}: {str(body)[:200]}")
            self._cache[code] = (str(resp.request.url), body[1])
        return self._cache[code]

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        iso3, code = symbol.split(":", 1)
        url, rows = self._indicator(code)
        mine = [
            {"date": r["date"], "value": r["value"]}
            for r in rows
            if r.get("countryiso3code") == iso3 and (since is None or int(r["date"]) >= since.year)
        ]
        payload = json.dumps({"country": iso3, "indicator": code, "rows": mine}).encode()
        return FetchResult(request=f"{url} [{iso3}]", payload=payload, content_type="application/json")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        body = json.loads(payload)
        if body.get("country") + ":" + body.get("indicator") != symbol:
            raise ProviderError(f"payload is for {body.get('country')}:{body.get('indicator')}, expected {symbol}")
        return [
            ParsedPoint(date(int(r["date"]), 1, 1), None if r["value"] is None else float(r["value"]))
            for r in body["rows"]
            if str(r["date"]).isdigit()
        ]

    def citation(self, symbol: str) -> str:
        iso3, code = symbol.split(":", 1)
        return f"World Bank WDI indicator {code} for {iso3}, {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        iso3, code = symbol.split(":", 1)
        return f"https://data.worldbank.org/indicator/{code}?locations={iso3}"
