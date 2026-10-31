"""World Bank Pink Sheet connector: finding the file, reading the workbook and failing loudly."""

import io
from datetime import date

import httpx
import openpyxl
import pytest
from fastapi.testclient import TestClient

from atlas.ingestion.pinksheet import COLUMNS, PinkSheetAdapter, read_table
from atlas.instruments import datasets
from atlas.provider_sdk import ProviderError

LINK = "https://thedocs.worldbank.org/en/doc/abc-001/related/CMO-Historical-Data-Monthly.xlsx"


def workbook(headers=("Gold", "Beef **", "Urea "), units=("($/troy oz)", "($/kg)", "($/mt)")) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Monthly Prices"
    ws.append(["World Bank Commodity Price Data (The Pink Sheet)"])
    ws.append(["Updated on October 02, 2026"])
    ws.append([None, *headers])
    ws.append([None, *units])
    ws.append(["1989M12", 400, 2.5, 100])
    ws.append(["2026M08", 4200, 6.8, "…"])
    ws.append(["2026M09", 4319, 6.86, 407.5])
    ws.append(["Note", None, None, None])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def adapter(xlsx: bytes, page: str = f'<a href="{LINK}">Monthly prices</a>') -> tuple[PinkSheetAdapter, list[str]]:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, text=page) if "worldbank.org/en/research" in str(req.url) else httpx.Response(200, content=xlsx)

    return PinkSheetAdapter(httpx.Client(transport=httpx.MockTransport(handler))), calls


def test_reads_a_column_from_the_linked_workbook_once():
    a, calls = adapter(workbook())
    fetched = a.fetch("CMO.GOLD")
    assert fetched.request == LINK
    assert a.parse("CMO.GOLD", fetched.payload)[-1].observed_at == date(2026, 9, 1)
    assert [p.value for p in a.parse("CMO.GOLD", fetched.payload)] == [4200, 4319]  # 1989 is left out
    a.fetch("CMO.UREA")
    assert len(calls) == 2  # one page and one workbook for both series


def test_footnote_marks_and_missing_values():
    table = read_table(workbook())
    assert table["Beef"][0] == "($/kg)"
    assert table["Urea"][1][1] == (date(2026, 8, 1), None)
    a, _ = adapter(workbook())
    assert a.parse("CMO.UREA", a.fetch("CMO.UREA").payload)[0].value is None


def test_since_limits_the_rows():
    a, _ = adapter(workbook())
    points = a.parse("CMO.GOLD", a.fetch("CMO.GOLD", since=date(2026, 9, 1)).payload)
    assert [p.observed_at for p in points] == [date(2026, 9, 1)]


def test_a_changed_unit_or_missing_column_fails():
    a, _ = adapter(workbook(units=("($/kg)", "($/kg)", "($/mt)")))
    with pytest.raises(ProviderError, match="expected"):
        a.fetch("CMO.GOLD")
    a, _ = adapter(workbook(headers=("Gold spot", "Beef", "Urea")))
    with pytest.raises(ProviderError, match="no column"):
        a.fetch("CMO.GOLD")


def test_a_missing_link_or_wrong_file_fails():
    a, _ = adapter(workbook(), page="<p>moved</p>")
    with pytest.raises(ProviderError, match="link"):
        a.fetch("CMO.GOLD")
    a, _ = adapter(b"not a workbook")
    with pytest.raises(ProviderError, match="unexpected"):
        a.fetch("CMO.GOLD")


def test_every_key_is_registered_with_a_licence():
    sourced = {d.provider_symbol: d for d in datasets().values() if d.provider_id == "worldbank_cmo"}
    assert set(sourced) == {f"CMO.{k}" for k in COLUMNS}
    assert all(d.meta["licence"]["class"] == "check_terms" for d in sourced.values())


def test_gold_is_served_as_a_monthly_average(seeded):
    from atlas.api.main import app

    with TestClient(app) as client:
        quote = client.get("/api/v1/markets/quotes?ids=CMD.GOLD").json()[0]
    assert quote["value"] > 0 and quote["is_tradable_quote"] is False
