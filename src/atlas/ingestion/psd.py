"""USDA Foreign Agricultural Service PSD connector (Production, Supply and Distribution).

Uses the public bulk CSV downloads (no API key): one zip per commodity group, refreshed by USDA
after each monthly WASDE report. Each ATLAS fundamentals dataset is one commodity's
country-by-market-year balance sheet; the adapter keeps the attributes needed for a supply and
demand table and, on incremental runs, only the last few market years (USDA revises those).
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

import httpx

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

BASE = "https://apps.fas.usda.gov/psdonline/downloads"
FIRST_MARKET_YEAR = 1990
REVISION_WINDOW = 3  # market years USDA still revises
MAX_ZIP_BYTES = 50_000_000
# PSD attribute → ATLAS attribute. Consumption differs by commodity group.
ATTRIBUTES = {
    "Production": "production", "Imports": "imports", "Exports": "exports", "Beginning Stocks": "beginning_stocks",
    "Ending Stocks": "ending_stocks", "Total Supply": "total_supply", "Domestic Consumption": "consumption",
    "Total Disappearance": "consumption", "Domestic Use": "consumption", "Area Harvested": "area_harvested",
}
COLUMNS = ["market_year", "country_code", "country_name", "attribute", "value", "release"]


def _config(symbol: str) -> dict[str, str]:
    from atlas.instruments import load_registry

    for c in load_registry().get("fundamentals", {}).get("commodities", []):
        if c["provider_symbol"] == symbol:
            return c
    raise ProviderError(f"unknown PSD commodity symbol '{symbol}'")


class PsdAdapter(ProviderAdapter):
    provider_id = "usda_psd"
    display_name = "USDA Foreign Agricultural Service, PSD Online"
    homepage = "https://apps.fas.usda.gov/psdonline/"
    licence_note = "U.S. government work; public domain."

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=120, headers={"User-Agent": settings.user_agent},
                                              follow_redirects=True)
        self._zips: dict[str, bytes] = {}  # one download per group per run (corn and wheat share a file)

    def _group_csv(self, group: str) -> tuple[str, str]:
        if group not in self._zips:
            url = f"{BASE}/psd_{group}_csv.zip"
            resp = with_retries(lambda: self._client.get(url))
            resp.raise_for_status()
            if len(resp.content) > MAX_ZIP_BYTES:
                raise ProviderError(f"PSD download for {group} is unexpectedly large ({len(resp.content)} bytes)")
            self._zips[group] = resp.content
        try:
            z = zipfile.ZipFile(io.BytesIO(self._zips[group]))
            member = next(n for n in z.namelist() if n.endswith(".csv") and "/" not in n)
            if z.getinfo(member).file_size > 20 * MAX_ZIP_BYTES:
                raise ProviderError("PSD CSV is unexpectedly large")
            return z.read(member).decode("utf-8-sig"), f"{BASE}/psd_{group}_csv.zip"
        except (zipfile.BadZipFile, StopIteration) as exc:
            raise ProviderError(f"PSD download for {group} is not a CSV zip") from exc

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        cfg = _config(symbol)
        text, url = self._group_csv(cfg["psd_group"])
        first_year = max(FIRST_MARKET_YEAR, since.year - REVISION_WINDOW) if since else FIRST_MARKET_YEAR
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(COLUMNS)
        n = 0
        for r in csv.DictReader(io.StringIO(text)):
            attr = ATTRIBUTES.get(r["Attribute_Description"].strip())
            if r["Commodity_Description"] != cfg["psd_commodity"] or attr is None or int(r["Market_Year"]) < first_year:
                continue
            w.writerow([r["Market_Year"], r["Country_Code"].strip(), r["Country_Name"].strip(), attr, r["Value"],
                        f"{r['Calendar_Year']}-{r['Month']}"])
            n += 1
        if n == 0:
            raise ProviderError(f"no PSD rows for '{cfg['psd_commodity']}' (file layout may have changed)")
        return FetchResult(request=url, payload=out.getvalue().encode(), content_type="text/csv")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8")))
        if reader.fieldnames != COLUMNS:
            raise ProviderError(f"unexpected PSD payload columns {reader.fieldnames!r}")
        points = []
        for r in reader:
            y, m = r["release"].split("-")
            points.append(ParsedPoint(
                observed_at=date(int(r["market_year"]), 1, 1), value=float(r["value"]),
                dims=(("country", r["country_code"]), ("country_name", r["country_name"]), ("attribute", r["attribute"])),
                fields={"release": int(y) * 100 + int(m)},
            ))
        return points

    def citation(self, symbol: str) -> str:
        return f"USDA FAS, Production, Supply and Distribution database: {_config(symbol)['psd_commodity']}"

    def source_url(self, symbol: str) -> str:
        return self.homepage
