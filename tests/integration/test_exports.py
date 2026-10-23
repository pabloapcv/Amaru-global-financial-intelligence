import csv
import io
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from atlas.api.main import app
from atlas.data.db import utcnow


def _csv(body: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    lines = body.splitlines()
    head = dict(ln[2:].split(": ", 1) for ln in lines if ln.startswith("# "))
    return head, list(csv.DictReader(io.StringIO("\n".join(ln for ln in lines if not ln.startswith("#")))))


@pytest.mark.parametrize("dataset_id, first_col", [
    ("CMD.SUGAR.ISA", "date"), ("US.CPI.HEADLINE", "date"), ("COT.SUGAR11", "report_date"),
    ("TRADE.BOL.USA", "year"), ("FUND.SUGAR", "market_year"),
])
def test_every_dataset_kind_exports_with_provenance(seeded, dataset_id, first_col):
    r = TestClient(app).get(f"/api/v1/datasets/{dataset_id}/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert f'filename="{dataset_id.lower()}-' in r.headers["content-disposition"]
    head, rows = _csv(r.text)
    assert head["dataset_id"] == dataset_id and head["citation"] and head["freshness"] and "licence" in head
    assert rows and list(rows[0])[0] == first_col


def test_filters_json_and_point_in_time(seeded):
    c = TestClient(app)
    full = _csv(c.get("/api/v1/datasets/CMD.SUGAR.ISA/export", params={"header": False}).text)[1]
    part = _csv(c.get("/api/v1/datasets/CMD.SUGAR.ISA/export", params={"start": full[-12]["date"]}).text)[1]
    assert len(part) == 12 and part[0]["date"] == full[-12]["date"]
    assert not c.get("/api/v1/datasets/CMD.SUGAR.ISA/export", params={"header": False}).text.startswith("#")
    j = c.get("/api/v1/datasets/COT.SUGAR11/export", params={"format": "json"}).json()
    assert j["provenance"]["dataset_id"] == "COT.SUGAR11" and "noncomm_net" in j["columns"]
    assert len(j["rows"][0]["report_date"]) == 10  # plain ISO date
    # Point in time: before anything was ingested there is nothing to export (no look-ahead).
    early = c.get("/api/v1/datasets/US.CPI.HEADLINE/export", params={"as_of": (utcnow() - timedelta(days=3650)).isoformat()})
    assert early.status_code == 409
    now = c.get("/api/v1/datasets/US.CPI.HEADLINE/export", params={"as_of": (utcnow() + timedelta(minutes=1)).isoformat()})
    assert now.status_code == 200 and "# as_of:" in now.text
    assert c.get("/api/v1/datasets/NOPE/export").status_code == 404


def test_exports_need_sign_in_and_are_audited(seeded, monkeypatch):
    from sqlalchemy import select

    from atlas.data.db import AuditEvent, session_scope

    monkeypatch.setenv("ATLAS_AUTH", "required")
    c = TestClient(app)
    assert c.get("/api/v1/datasets/CMD.SUGAR.ISA/export").status_code == 401
    r = c.post("/api/v1/auth/signup", json={"email": "exporter@example.test", "password": "correct horse battery", "workspace_name": "Exports"})
    ws = r.json()["workspace"]["workspace_id"]
    assert c.get("/api/v1/datasets/FUND.SUGAR/export", params={"format": "json"}).status_code == 200
    with session_scope() as s:
        ev = s.execute(select(AuditEvent).where(AuditEvent.workspace_id == ws, AuditEvent.action == "data.exported")).scalars().all()
    assert ev and ev[-1].detail == {"dataset_id": "FUND.SUGAR", "format": "json"}


def test_every_sourced_dataset_has_a_licence_and_commercial_mode_withholds_restricted(seeded, monkeypatch):
    from atlas import instruments
    from atlas.data.access import DataUnavailable, get_series, series_meta
    from atlas.research import tools

    classes = {"public_domain", "cc_by", "check_terms", "third_party_restricted"}
    for ds in instruments.datasets().values():
        if ds.has_source:
            assert (ds.meta.get("licence") or {}).get("class") in classes, ds.dataset_id
    assert series_meta("EQ.US.SP500").licence["class"] == "third_party_restricted"
    assert "# licence: Restricted (third party): S&P Dow Jones Indices" in TestClient(app).get("/api/v1/datasets/EQ.US.SP500/export").text
    assert get_series("EQ.US.SP500")[0].size > 0  # normal mode: available

    monkeypatch.setenv("ATLAS_COMMERCIAL_MODE", "true")
    instruments.datasets.cache_clear()
    try:
        assert not instruments.get_dataset("EQ.US.SP500").has_source
        assert series_meta("VOL.US.VIX").freshness == "no_source"
        with pytest.raises(DataUnavailable):
            get_series("EQ.US.SP500")  # stored rows are not served either
        out, err = tools.execute(tools.ToolContext(), "get_latest_quote", {"dataset_id": "EQ.US.NASDAQ"})
        assert err and "commercial deployment" in out
        assert TestClient(app).get("/api/v1/datasets/EQ.US.SP500/export").status_code == 409
        assert get_series("CMD.CRUDE.BRENT")[0].size > 0  # public-domain data unaffected
    finally:
        monkeypatch.delenv("ATLAS_COMMERCIAL_MODE")
        instruments.datasets.cache_clear()
