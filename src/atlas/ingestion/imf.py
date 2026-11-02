"""IMF connectors (public APIs, no key).

- ImfCpiAdapter ("imf"): monthly consumer price inflation, year on year, from the IMF's SDMX API
  (dataset IMF.STA:CPI). Symbols are "CPI.<ISO3>". The IMF's own year-on-year figure is used
  rather than the index, because each country's index has its own base period and a rebasing
  would otherwise look like a revision of the whole history.
- WeoAdapter ("imf_weo"): the World Economic Outlook, actuals and IMF projections, from the
  DataMapper API. Symbols are "<WEO indicator>.<ISO3>". The API returns every country for an
  indicator at once, so each indicator is downloaded once per adapter instance and only the
  requested country is stored.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

CPI_URL = "https://api.imf.org/external/sdmx/2.1/data/IMF.STA,CPI/{iso3}.CPI._T.YOY_PCH_PA_PT.M"
WEO_URL = "https://www.imf.org/external/datamapper/api/v1/{indicator}"
FIRST_MONTH = "1990-01"
LICENCE_NOTE = "IMF data; cite the IMF as the source and check the IMF's copyright and usage terms before redistributing."


def _client(client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent}, follow_redirects=True)


class ImfCpiAdapter(ProviderAdapter):
    provider_id = "imf"
    display_name = "International Monetary Fund, consumer price index (SDMX)"
    homepage = "https://data.imf.org"
    licence_note = LICENCE_NOTE

    def __init__(self, client: httpx.Client | None = None):
        self._client = _client(client)

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        start = since.strftime("%Y-%m") if since else FIRST_MONTH
        resp = with_retries(lambda: self._client.get(CPI_URL.format(iso3=symbol.split(".", 1)[1]), params={"startPeriod": start}))
        return FetchResult(request=str(resp.request.url), payload=resp.content, content_type="application/xml")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        iso3 = symbol.split(".", 1)[1]
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise ProviderError(f"unexpected IMF payload for {symbol}: {payload[:200]!r}") from exc
        series = [el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "Series"]
        if len(series) > 1:
            raise ProviderError(f"IMF returned {len(series)} series for {symbol}, expected one")
        out = []
        for s in series:
            if s.get("COUNTRY") != iso3 or s.get("TYPE_OF_TRANSFORMATION") != "YOY_PCH_PA_PT" or s.get("FREQUENCY") != "M":
                raise ProviderError(f"IMF series for {symbol} is {dict(s.attrib)}, not monthly year-on-year CPI for {iso3}")
            for obs in s:
                period, value = obs.get("TIME_PERIOD", ""), obs.get("OBS_VALUE")
                year, _, month = period.partition("-M")
                if not (year.isdigit() and month.isdigit()):
                    raise ProviderError(f"unexpected IMF period {period!r} for {symbol}")
                out.append(ParsedPoint(date(int(year), int(month), 1), None if value in (None, "", "NaN") else round(float(value), 4)))
        return sorted(out, key=lambda p: p.observed_at)

    def citation(self, symbol: str) -> str:
        return f"IMF consumer price index, {symbol.split('.', 1)[1]}, year-on-year % change, monthly (IMF.STA:CPI), {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        return "https://data.imf.org/en/datasets/IMF.STA:CPI"


class WeoAdapter(ProviderAdapter):
    provider_id = "imf_weo"
    display_name = "International Monetary Fund, World Economic Outlook (DataMapper)"
    homepage = "https://www.imf.org/external/datamapper"
    licence_note = LICENCE_NOTE

    def __init__(self, client: httpx.Client | None = None):
        self._client = _client(client)
        self._indicators: dict[str, dict[str, Any]] = {}

    def _indicator(self, indicator: str) -> tuple[dict[str, Any], str]:
        url = WEO_URL.format(indicator=indicator)
        if indicator not in self._indicators:
            resp = with_retries(lambda: self._client.get(url))
            try:
                self._indicators[indicator] = json.loads(resp.content)["values"][indicator]
            except (ValueError, KeyError, TypeError) as exc:
                raise ProviderError(f"unexpected IMF DataMapper payload for {indicator}: {resp.content[:200]!r}") from exc
        return self._indicators[indicator], url

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        indicator, iso3 = symbol.split(".", 1)
        values, url = self._indicator(indicator)
        if iso3 not in values:
            raise ProviderError(f"the World Economic Outlook has no {indicator} for {iso3}")
        payload = json.dumps({"indicator": indicator, "country": iso3, "values": values[iso3]}).encode()
        return FetchResult(request=url, payload=payload, content_type="application/json")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        indicator, iso3 = symbol.split(".", 1)
        body = json.loads(payload)
        if body.get("indicator") != indicator or body.get("country") != iso3:
            raise ProviderError(f"payload is {body.get('indicator')}/{body.get('country')}, expected {indicator}/{iso3}")
        return sorted((ParsedPoint(date(int(y), 1, 1), None if v is None else float(v)) for y, v in body["values"].items()),
                      key=lambda p: p.observed_at)

    def citation(self, symbol: str) -> str:
        indicator, iso3 = symbol.split(".", 1)
        return f"IMF World Economic Outlook, {indicator}, {iso3} (actuals and IMF projections), {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        indicator, iso3 = symbol.split(".", 1)
        return f"https://www.imf.org/external/datamapper/{indicator}@WEO/{iso3}"
