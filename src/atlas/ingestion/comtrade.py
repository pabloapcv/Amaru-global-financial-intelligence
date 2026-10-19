"""UN Comtrade connector (public preview API, no key).

The preview API allows one period per request and roughly one request per second, so the
adapter paces requests and bundles a pair's history into a single raw payload:
  - TOTAL exports and imports for each of the last N years (one request per year)
  - HS 2-digit chapter breakdown for the latest year with data (one request)
Values are as declared by the reporter (exports FOB, imports usually CIF).
"""

from __future__ import annotations

import json
import re
import time
from datetime import date

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

BASE = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
PACING_SECONDS = 1.2
MAX_QUOTA_WAIT = 300  # seconds


def quota_refill_seconds(message: str) -> int | None:
    """Seconds until Comtrade's call quota refills, parsed from its 403 message; None if not a quota error."""
    m = re.search(r"Out of call volume quota.*?replenished in (\d+):(\d+):(\d+)", message, re.S)
    if not m:
        return None
    h, mnt, sec = (int(x) for x in m.groups())
    return h * 3600 + mnt * 60 + sec


def _m49(iso3: str) -> int:
    if iso3 == "WLD":
        return 0
    from atlas.instruments import countries

    for c in countries():
        if c["iso3"] == iso3:
            return int(c["m49"])
    raise ProviderError(f"no M49 code for {iso3}")


class ComtradeAdapter(ProviderAdapter):
    provider_id = "comtrade"
    display_name = "UN Comtrade"
    homepage = "https://comtradeplus.un.org"
    licence_note = "UN Comtrade terms of use; public preview API."

    def __init__(self, client: httpx.Client | None = None, pacing: float = PACING_SECONDS):
        self._client = client or httpx.Client(
            timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent}
        )
        self._pacing = pacing
        self._last_call = 0.0
        self._sleep = time.sleep

    def _get(self, params: dict) -> list[dict]:
        wait = self._pacing - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            resp = with_retries(lambda: self._client.get(BASE, params=params), base_delay=3.0)
        except ProviderError as exc:
            # Comtrade's quota error says when the quota refills ("replenished in 00:03:03"); wait and retry
            # once when that is short, instead of failing the refresh.
            refill = quota_refill_seconds(str(exc))
            if refill is None or refill > MAX_QUOTA_WAIT:
                raise
            self._sleep(refill + 2)
            resp = with_retries(lambda: self._client.get(BASE, params=params), base_delay=3.0)
        self._last_call = time.monotonic()
        body = resp.json()
        if body.get("error"):
            raise ProviderError(f"Comtrade error: {body['error']}")
        return body.get("data") or []

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        from atlas.instruments import load_registry

        reporter, partner = symbol.split(":")
        years = int(load_registry().get("trade_pairs", {}).get("years", 8))
        last = date.today().year - 1
        first = max(last - years + 1, since.year if since else 0)
        base = {"reporterCode": _m49(reporter), "partnerCode": _m49(partner), "flowCode": "X,M"}
        totals = []
        for year in range(first, last + 1):
            for r in self._get({**base, "period": str(year), "cmdCode": "TOTAL"}):
                totals.append({"year": year, "flow": r["flowCode"], "cmd": "TOTAL", "value": r.get("primaryValue")})
        products: list[dict] = []
        latest = max((t["year"] for t in totals if t["value"]), default=None)
        if latest:
            for r in self._get({**base, "period": str(latest), "cmdCode": "AG2"}):
                products.append({"year": latest, "flow": r["flowCode"], "cmd": r["cmdCode"], "value": r.get("primaryValue")})
        bundle = {"reporter": reporter, "partner": partner, "rows": totals + products}
        return FetchResult(request=f"{BASE} reporter={reporter} partner={partner} years={first}-{last}",
                           payload=json.dumps(bundle).encode(), content_type="application/json")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        body = json.loads(payload)
        if f"{body.get('reporter')}:{body.get('partner')}" != symbol:
            raise ProviderError(f"payload is for {body.get('reporter')}:{body.get('partner')}, expected {symbol}")
        return [
            ParsedPoint(date(int(r["year"]), 1, 1), None if r["value"] is None else float(r["value"]),
                        dims=(("flow", r["flow"]), ("cmd", str(r["cmd"]))))
            for r in body["rows"]
            if r["flow"] in ("X", "M")
        ]

    def citation(self, symbol: str) -> str:
        reporter, partner = symbol.split(":")
        return f"UN Comtrade (HS, annual), reporter {reporter}, partner {partner}, {self.source_url(symbol)}"

    def source_url(self, symbol: str) -> str:
        return "https://comtradeplus.un.org/TradeFlow"
