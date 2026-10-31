"""World Bank commodity prices connector ("The Pink Sheet", no key).

The World Bank publishes one workbook of monthly average prices, updated early each month, at a
link that changes with each edition, so the link is read from its commodity markets page. The
workbook holds every commodity, so fetch keeps only the requested column, as a small CSV, instead
of storing the whole file once per series. An instance downloads the workbook once.

Symbols are "CMO.<key>". Each key states the file's column and the unit it must be in, so a column
that is renamed or changes unit fails loudly rather than publishing wrong prices.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from itertools import chain
from zipfile import BadZipFile

import httpx
import openpyxl

from atlas.config import settings
from atlas.provider_sdk import FetchResult, ParsedPoint, ProviderAdapter, ProviderError, with_retries

PAGE = "https://www.worldbank.org/en/research/commodity-markets"
FILE_LINK = re.compile(r"https://thedocs\.worldbank\.org/[^\"'\s]*CMO-Historical-Data-Monthly\.xlsx")
SHEET = "Monthly Prices"
# Older history mixes fixed prices and annual contracts; the IMF series start here too.
FIRST_MONTH = date(1990, 1, 1)
COLUMNS = {
    "GOLD": ("Gold", "($/troy oz)"), "SILVER": ("Silver", "($/troy oz)"), "PLATINUM": ("Platinum", "($/troy oz)"),
    "ALUMINUM": ("Aluminum", "($/mt)"), "IRON_ORE": ("Iron ore, cfr spot", "($/dmtu)"), "TIN": ("Tin", "($/mt)"), "ZINC": ("Zinc", "($/mt)"),
    "ROBUSTA": ("Coffee, Robusta", "($/kg)"), "SOYBEAN_OIL": ("Soybean oil", "($/mt)"), "SOYBEAN_MEAL": ("Soybean meal", "($/mt)"),
    "PALM_OIL": ("Palm oil", "($/mt)"), "RICE": ("Rice, Thai 5%", "($/mt)"), "COTTON": ("Cotton, A Index", "($/kg)"),
    "BEEF": ("Beef", "($/kg)"), "BANANA": ("Banana, US", "($/kg)"),
    "UREA": ("Urea", "($/mt)"), "DAP": ("DAP", "($/mt)"), "POTASH": ("Potassium chloride", "($/mt)"),
}


def _clean(header: object) -> str:
    return " ".join(str(header or "").replace("*", " ").split())


def read_table(workbook: bytes) -> dict[str, tuple[str, list[tuple[date, float | None]]]]:
    """Every column of the monthly sheet: cleaned header -> (unit, [(first of month, value)])."""
    try:
        sheet = openpyxl.load_workbook(io.BytesIO(workbook), read_only=True, data_only=True)[SHEET]
    except (KeyError, ValueError, OSError, BadZipFile, openpyxl.utils.exceptions.InvalidFileException) as exc:
        raise ProviderError(f"unexpected World Bank commodity workbook: {exc}") from exc
    rows = sheet.iter_rows(values_only=True)
    months = re.compile(r"^(\d{4})M(\d{2})$")
    headers: tuple = ()
    units: tuple = ()
    for row in rows:
        if row and months.match(str(row[0] or "")):
            break
        headers, units = units, row  # the two rows above the first month
    else:
        raise ProviderError("World Bank commodity workbook has no monthly rows")
    columns = [(i, _clean(h)) for i, h in enumerate(headers) if i and h]
    data: dict[str, list[tuple[date, float | None]]] = {name: [] for _, name in columns}
    for row in chain([row], rows):
        m = months.match(str(row[0] or ""))
        if not m:
            continue
        month = date(int(m[1]), int(m[2]), 1)
        for i, name in columns:
            data[name].append((month, float(row[i]) if isinstance(row[i], int | float) else None))
    return {name: (str(units[i] or "").strip(), data[name]) for i, name in columns}


class PinkSheetAdapter(ProviderAdapter):
    provider_id = "worldbank_cmo"
    display_name = "World Bank Commodity Price Data (The Pink Sheet)"
    homepage = PAGE
    licence_note = "World Bank data is generally CC BY 4.0, but some of these prices come from third parties."

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=settings.http_timeout, headers={"User-Agent": settings.user_agent}, follow_redirects=True
        )
        self._source: tuple[str, dict] | None = None

    def _load(self) -> tuple[str, dict]:
        if self._source is None:
            page = with_retries(lambda: self._client.get(PAGE))
            link = FILE_LINK.search(page.text)
            if not link:
                raise ProviderError("no monthly workbook link on the World Bank commodity markets page")
            url = link.group(0)
            self._source = (url, read_table(with_retries(lambda: self._client.get(url)).content))
        return self._source

    def fetch(self, symbol: str, since: date | None = None) -> FetchResult:
        header, unit = COLUMNS[symbol.split(".", 1)[1]]
        url, table = self._load()
        if header not in table:
            raise ProviderError(f"World Bank commodity workbook has no column '{header}'")
        found, points = table[header]
        if found != unit:
            raise ProviderError(f"'{header}' is in {found}, expected {unit}")
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["month", "value"])
        start = max(since or FIRST_MONTH, FIRST_MONTH)
        writer.writerows((m.isoformat(), "" if v is None else v) for m, v in points if m >= start)
        return FetchResult(request=url, payload=out.getvalue().encode(), content_type="text/csv")

    def parse(self, symbol: str, payload: bytes) -> list[ParsedPoint]:
        rows = csv.DictReader(io.StringIO(payload.decode()))
        if rows.fieldnames != ["month", "value"]:
            raise ProviderError(f"unexpected World Bank payload for {symbol}: {payload[:200]!r}")
        return [ParsedPoint(date.fromisoformat(r["month"]), float(r["value"]) if r["value"] else None) for r in rows]

    def citation(self, symbol: str) -> str:
        return f"World Bank Commodity Price Data (The Pink Sheet), {COLUMNS[symbol.split('.', 1)[1]][0]}, monthly average, {PAGE}"

    def source_url(self, symbol: str) -> str:
        return PAGE
