"""Scenario agent: the scan, the Scenario Outlook report and its API options."""

import pytest
from fastapi.testclient import TestClient


def test_scan_ranks_signalled_scenarios_and_measures_effects(seeded):
    from atlas import scenarios

    res, metas = scenarios.scan(custom=[{"driver": "FX.USDBRL", "shock": 10, "name": "Real weakens 10%"}])
    assert res["scenarios"][0]["name"] == "Real weakens 10%" and res["scenarios"][0]["kind"] == "custom"  # yours come first
    assert 2 <= len(res["scenarios"]) <= scenarios.MAX_SCENARIOS and metas
    for sc in res["scenarios"]:
        if sc["kind"] != "custom":
            assert sc["relevance"] == len(sc["signals"])
        driver_rows = [i for i in sc["impacts"] if i["is_driver"]]
        assert all(i["implied_move"] == sc["shock"] for i in driver_rows)
        for i in sc["impacts"]:
            if not i["is_driver"]:
                lo, hi = i["ci95"]
                assert lo <= i["implied_move"] <= hi
                assert i["significant"] == (i["p_value"] is not None and i["p_value"] <= 0.05)
        assert set(sc["clearest_effects"]) <= {i["dataset_id"] for i in sc["impacts"] if i["significant"]}
    assert "not a probability" in res["method"]["caveat"]
    ranked = [s["relevance"] for s in res["scenarios"] if s["kind"] != "custom"]
    assert ranked == sorted(ranked, reverse=True)

    with pytest.raises(scenarios.ScenarioError):
        scenarios.scan(universe=["NOPE.X"])
    with pytest.raises(scenarios.ScenarioError):
        scenarios.scan(custom=[{"driver": "FX.USDBRL", "shock": 0}])
    with pytest.raises(scenarios.ScenarioError):
        scenarios.scan(universe=[f"X{i}" for i in range(16)])


def test_signal_rules_fire_on_extremes(seeded, monkeypatch):
    from atlas import scenarios

    real = scenarios._perf
    monkeypatch.setattr(scenarios, "_perf", lambda s, d: {**real(s, d), "pct_3m": 6.0, "percentile_5y": 95.0} if d == "FX.USD.BROAD" else real(s, d))
    res, _ = scenarios.scan(universe=["CMD.CRUDE.BRENT", "EQ.US.SP500"])
    dollar_up = next(s for s in res["scenarios"] if s["id"] == "dollar_up")
    assert [g["text"] for g in dollar_up["signals"]] == ["Dollar moved +6.0% over 3 months"]
    dollar_down = next(s for s in res["scenarios"] if s["id"] == "dollar_down")
    assert dollar_down["signals"][0]["text"] == "Dollar sits at the 95th percentile of its 5-year range"
    assert scenarios._ordinal(1) == "1st" and scenarios._ordinal(62.4) == "62nd" and scenarios._ordinal(13) == "13th"


def test_scenario_outlook_report_is_verified_and_api_takes_options(seeded):
    from atlas.research import engine

    run_id = engine.create_run("Scenario outlook", {"engine": "deterministic", "template": "scenario_outlook", "formats": [],
                                                    "scenario_universe": ["CMD.CRUDE.BRENT", "CMD.SUGAR.ISA", "FX.USDBRL", "EQ.US.SP500"],
                                                    "custom_scenarios": [{"driver": "RATE.UST.10Y", "shock": 50, "name": "Long yields +50 bp"}]})
    res = engine.execute_run(run_id)
    assert res["verification"]["passed"], res["verification"]["unverified_numbers"][:5]
    scan = next(e for e in res["evidence"] if e["tool"] == "scan_scenarios")["result"]
    assert scan["universe"] == ["CMD.CRUDE.BRENT", "CMD.SUGAR.ISA", "FX.USDBRL", "EQ.US.SP500"]
    md = res["narrative_markdown"]
    assert md.startswith("# Scenario Outlook") and "## Scenarios" in md and "### 1. Long yields +50 bp" in md and "not a probability" in md

    from atlas.api.main import app

    with TestClient(app) as c:
        bad = c.post("/api/v1/research", json={"query": "Scenario outlook", "report": {"template": "scenario_outlook"},
                                               "scenarios": {"custom": [{"driver": "NOPE", "shock": 5}]}})
        assert bad.status_code == 422
        ok = c.post("/api/v1/research", json={"query": "Scenario outlook please", "engine": "deterministic", "report": {"template": "scenario_outlook", "formats": []},
                                              "scenarios": {"universe": ["CMD.CRUDE.BRENT", "EQ.US.SP500"]}, "no_cache": True})
        assert ok.status_code == 202
        sched = c.post("/api/v1/schedules", json={"name": "Weekly scenarios", "query": "Scenario outlook", "template": "scenario_outlook",
                                                  "cadence": "weekly", "scenarios": {"custom": [{"driver": "FX.USDBRL", "shock": 10}]}}).json()
        assert sched["config"]["template"] == "scenario_outlook"
        assert sched["config"]["custom_scenarios"] == [{"driver": "FX.USDBRL", "shock": 10.0, "name": ""}]
        assert any(t["key"] == "scenario_outlook" for t in c.get("/api/v1/report-templates").json())


def test_scans_report_changes_and_notify_scheduled_owners(seeded, monkeypatch):
    from atlas import auth, mailer, scenarios, schedules, webhooks
    from atlas.research import engine

    sent, hooks = [], []
    ws = None  # set below; other tests' background runs may emit for other workspaces
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, **kw: (subject.startswith("ATLAS scenario agent") and sent.append((to, body))) or True)
    monkeypatch.setattr(webhooks, "emit", lambda ws_, event, data: (event == "scenarios.changed" and ws_ == ws and hooks.append((ws_, event, data))) or [])
    uid, ws = auth.create_user("watcher@scen.test", "correct horse battery", workspace_name="Watchers")
    config = {"engine": "deterministic", "template": "scenario_outlook", "formats": [], "scenario_universe": ["CMD.CRUDE.BRENT", "EQ.US.SP500"]}

    real = scenarios._perf

    def dollar(pct: float):  # pin the dollar's readings; the synthetic data differs from session to session
        monkeypatch.setattr(scenarios, "_perf", lambda s, d: {**real(s, d), "pct_3m": pct, "percentile_5y": 50.0} if d == "FX.USD.BROAD" else real(s, d))

    dollar(0.0)
    first = engine.create_run("Scenario outlook", dict(config), uid, ws)
    res = engine.execute_run(first)
    scan1 = next(e for e in res["evidence"] if e["tool"] == "scan_scenarios")["result"]
    assert scan1["changes"] is None and hooks == []  # nothing to compare with yet

    # The dollar breaks out: a new scenario appears in the next (scheduled) scan.
    dollar(6.0)
    assert not any(s["id"] == "dollar_up" and s["relevance"] for s in scan1["scenarios"])
    sched = schedules.create_schedule("Weekly scenarios", "Scenario outlook", "weekly", dict(config), workspace_id=ws, user_id=uid)
    second = schedules.run_schedule(sched["schedule_id"], background=False)
    run = engine.get_run(second, ws)
    assert run["result"]["verification"]["passed"], run["result"]["verification"]["unverified_numbers"][:5]
    scan2 = next(e for e in run["result"]["evidence"] if e["tool"] == "scan_scenarios")["result"]
    ch = scan2["changes"]
    assert ch["previous_run_id"] == first and ch["changed"]
    assert [x["id"] for x in ch["new"]] == ["dollar_up"] and ch["new"][0]["relevance"] == 1  # 0 → 1 signal counts as new
    assert "### What changed since the last scan" in run["result"]["narrative_markdown"]
    assert [e for _, e, _ in hooks] == ["scenarios.changed"] and hooks[0][0] == ws
    assert sent and sent[0][0] == "watcher@scen.test" and "Dollar +5%" in sent[0][1] and "not a probability" in sent[0][1]

    # Same data again: nothing new, so no notice.
    hooks.clear()
    sent.clear()
    third = schedules.run_schedule(sched["schedule_id"], background=False)
    scan3 = next(e for e in engine.get_run(third, ws)["result"]["evidence"] if e["tool"] == "scan_scenarios")["result"]
    assert scan3["changes"]["changed"] is False and not any(s.get("is_new") for s in scan3["scenarios"])
    assert hooks == [] and sent == []
    assert "scenarios.changed" in webhooks.EVENTS
