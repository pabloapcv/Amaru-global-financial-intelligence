"""Central bank policy rates: the BIS connector, the moves and direction, and where they surface."""

from datetime import date

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

HEADER = "DATAFLOW,FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE"


def bis_csv(area: str, rows: list[tuple[str, str]]) -> bytes:
    return "\n".join([HEADER] + [f"BIS:WS_CBPOL(1.0),D,{area},{d},{v}" for d, v in rows]).encode() + b"\n"


def steps(moves: dict[str, float], start: str = "2024-01-01", end: str = "2026-10-20", first: float = 5.0) -> pd.Series:
    idx = pd.date_range(start, end)
    return (pd.Series(0.0, index=idx).add(pd.Series(list(moves.values()), index=pd.to_datetime(list(moves)), dtype=float), fill_value=0).cumsum() + first).rename("rate")


def test_adapter_keeps_gaps_and_refuses_the_wrong_area():
    from atlas.ingestion.bis import BisAdapter
    from atlas.provider_sdk import ProviderError

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, content=bis_csv("BR", [("2026-10-26", "13.75"), ("2026-10-27", "")]))

    a = BisAdapter(httpx.Client(transport=httpx.MockTransport(handler)))
    got = a.fetch("CBPOL.BR", since=date(2026, 10, 1))
    assert seen[0].path.endswith("/D.BR") and seen[0].params["startPeriod"] == "2026-10-01" and seen[0].params["detail"] == "dataonly"
    points = a.parse("CBPOL.BR", got.payload)
    assert [(p.observed_at.isoformat(), p.value) for p in points] == [("2026-10-26", 13.75), ("2026-10-27", None)]
    with pytest.raises(ProviderError, match="expected MX"):
        a.parse("CBPOL.MX", got.payload)
    with pytest.raises(ProviderError, match="unexpected"):
        a.parse("CBPOL.BR", b"<error>no data</error>")


def test_a_bank_reports_dated_moves_and_its_direction():
    from atlas import policy_rates

    area = {"code": "BR", "name": "Brazil", "countries": ["BRA"]}
    today = date(2026, 10, 25)
    b = policy_rates._bank(area, steps({"2025-06-10": 0.5, "2026-06-18": -0.25, "2026-09-17": -0.25}), today)
    assert b["rate"] == 5.0 and b["last_move"] == {"date": "2026-09-17", "change_bp": -25.0}
    assert b["change_3m_bp"] == -25.0 and b["change_12m_bp"] == -50.0
    assert (b["cuts_12m"], b["hikes_12m"], b["direction"], b["stale"]) == (2, 0, "cutting", False)

    still = policy_rates._bank(area, steps({"2024-06-10": 0.25}), today)
    assert still["direction"] == "on hold" and still["last_move"]["date"] == "2024-06-10" and still["cuts_12m"] == 0

    stopped = policy_rates._bank(area, steps({}, end="2025-06-30"), today)  # how Argentina's series ended
    assert stopped["stale"] and stopped["last_move"] is None

    short = policy_rates._bank(area, steps({}, start="2026-09-01"), today)
    assert short["direction"] is None and short["change_12m_bp"] is None


def test_late_banks_stay_current_in_the_data_status():
    from datetime import timedelta

    from atlas.data.access import _freshness
    from atlas.instruments import get_dataset

    ds = get_dataset("BIS.POLICY.KR")
    assert _freshness(ds, date.today() - timedelta(days=23), None)[0] == "current"
    assert _freshness(ds, date.today() - timedelta(days=61), None)[0] == "stale"


def test_direction_counts_each_bank_once_and_needs_enough_banks():
    from atlas import policy_rates

    cut, hike, flat = steps({"2026-03-02": -0.5}), steps({"2026-03-02": 0.25}), steps({})
    series = {**{f"C{i}": cut for i in range(6)}, **{f"H{i}": hike for i in range(3)}, "F": flat}
    path = policy_rates._net_path(series)
    assert {"date": "2026-05-31", "value": -30.0} in path and path[0]["date"] == "2024-07-31"
    assert policy_rates._net_path({"A": cut, "B": hike}) == []


def test_narrowing_to_a_country_keeps_the_worldwide_count(seeded):
    from atlas import policy_rates

    every = policy_rates.overview()
    assert len(every["banks"]) == 19 and every["direction"]["banks"] > 0
    only = policy_rates.overview(["FRA", "BOL", "ARG"])
    assert [b["name"] for b in only["banks"]] == ["Euro area"]
    assert only["direction"]["banks"] == every["direction"]["banks"]
    assert {n["country"]: n["reason"] for n in only["not_covered"]} == {"ARG": "the BIS series stopped in June 2025", "BOL": "not in the BIS data"}


def test_policy_rates_reach_the_api_and_a_verified_report(seeded):
    from atlas.api.main import app
    from atlas.research import engine
    from atlas.research.planner import build_plan

    with TestClient(app) as c:
        body = c.get("/api/v1/macro/policy-rates", params={"countries": "bra"}).json()
    assert body["available"] and [b["area"] for b in body["banks"]] == ["BR"] and body["direction"]["path"] and "sources" not in body

    plan = build_plan("What is Brazil's policy rate doing, and is the selic still falling?")
    assert any(s["tool"] == "get_policy_rates" and s["args"] == {"countries": ["BRA"]} for s in plan["steps"])

    run = engine.create_run("Where are central bank policy rates heading?", {"engine": "deterministic", "formats": []})
    res = engine.execute_run(run)
    assert res["verification"]["passed"], res["verification"]["unverified_numbers"][:5]
    assert "## Central bank policy rates" in res["narrative_markdown"]
    ev = next(e for e in res["evidence"] if e["tool"] == "get_policy_rates")
    assert "path" not in ev["result"]["direction"]  # the chart carries it
    assert any(ch["title"].startswith("Central banks raising") for ch in res["charts"])


def rate_rows(area: str, moved_on: date, before: float, after: float) -> bytes:
    days = pd.date_range(end=pd.Timestamp(date.today()), periods=60)
    return bis_csv(area, [(d.date().isoformat(), f"{before if d.date() < moved_on else after:.2f}") for d in days])


def test_a_central_bank_move_fires_an_agent_once_even_if_reported_late(seeded):
    from datetime import timedelta

    from atlas import agents
    from atlas.ingestion import pipeline
    from atlas.instruments import get_dataset

    with pytest.raises(agents.AgentError, match="no policy rate for atlantis"):
        agents.validate([], [{"type": "policy_rate_move", "banks": "Brazil, Atlantis"}], {"query": "why?"})
    a = agents.create("Latin America rates", [], [{"type": "policy_rate_move", "banks": "Mexico, Peru"}],
                      {"query": "What does this decision mean?", "engine": "deterministic"}, 1, False, "local", "local")
    assert agents.check_agent(a["agent_id"], background=False)["fired"] is False  # moves stored before the agent existed don't count

    # A decision from three weeks ago reaches Amaru only now, as the BIS's late reporting would.
    moved_on = date.today() - timedelta(days=21)
    ds = get_dataset("BIS.POLICY.PE")
    res = pipeline.ingest_dataset(ds, adapter=type(seeded)(overrides={"CBPOL.PE": rate_rows("PE", moved_on, 7.25, 7.0)}))
    assert res.status == "success" and agents.evaluate_agents({"FX.USD.BROAD"}) == []
    r = agents.check_agent(a["agent_id"], background=False)
    assert r["fired"], r
    f = next(x for x in r["findings"] if x["dataset_id"] == "BIS.POLICY.PE")
    assert f["message"] == f"Peru: policy rate cut 25 bp to 7% on {moved_on.isoformat()}" and f["value"] == -25.0
    assert not agents.check_agent(a["agent_id"], background=False, force=True)["fired"]  # same decision, no second report
    # "Check now" looks back further, but not past a decision already reported.
    assert not any(x["dataset_id"] == "BIS.POLICY.PE" for x in agents.check_agent(a["agent_id"], background=False, force=True, manual=True)["findings"])

    other = agents.create("Asia rates", [], [{"type": "policy_rate_move", "banks": "Japan"}],
                          {"query": "What does this decision mean?", "engine": "deterministic"}, 1, False, "local", "local")
    pipeline.ingest_dataset(ds, adapter=type(seeded)(overrides={"CBPOL.PE": rate_rows("PE", moved_on + timedelta(days=7), 7.0, 6.75)}))
    assert not any(f["dataset_id"] == "BIS.POLICY.PE" for f in agents.check_agent(other["agent_id"], background=False)["findings"])


def test_a_global_lean_counts_as_a_rate_signal(seeded, monkeypatch):
    from atlas import policy_rates, scenarios

    real = policy_rates.overview

    def leaning(hiking: int, cutting: int):
        def fake(*a, **k):
            out = real(*a, **k)
            return {**out, "direction": {**out["direction"], "banks": 19, "hiking": hiking, "cutting": cutting, "on_hold": 19 - hiking - cutting}}
        monkeypatch.setattr(policy_rates, "overview", fake)

    def signals(sid: str) -> list[str]:
        scan, _ = scenarios.scan(["CMD.CRUDE.BRENT"], max_scenarios=8)
        return [g["text"] for s in scan["scenarios"] if s["id"] == sid for g in s["signals"]]

    leaning(10, 4)
    assert "Over 6 months, 10 of 19 central banks raised rates and 4 cut" in signals("rates_up")
    assert not any("central banks" in t for t in signals("rates_down"))
    leaning(9, 5)  # 21% of banks: not enough of a lean
    assert not any("central banks" in t for t in signals("rates_up"))
