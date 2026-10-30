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


def _monthly(values, start="2010-01-31"):
    import pandas as pd

    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="ME"), dtype=float)


def test_past_episodes_do_not_count_one_move_twice():
    from atlas import analytics

    # Up 10% a month for six months: the threshold is reached in month two, and the search resumes three months later.
    levels = _monthly([100.0] * 6 + [100 * 1.1 ** i for i in range(1, 7)] + [177.0] * 6 + [130.0, 120.0, 110.0] + [110.0] * 4)
    up = analytics.past_episodes(levels, 20, "log_return")
    assert up["n"] == 2 and up["episodes"][0]["end"] == "2010-08-31" and up["episodes"][1]["end"] == "2010-11-30"
    assert up["episodes"][0]["start"] == "2010-05-31" and up["episodes"][0]["driver_move"] == pytest.approx(21, abs=0.01)
    down = analytics.past_episodes(levels, -30, "log_return")
    assert [e["end"] for e in down["episodes"]] == ["2011-08-31"]

    rates = _monthly([2.0, 2.0, 2.0, 2.6, 2.6, 2.6, 2.6, 2.6])
    assert analytics.past_episodes(rates, 50, "diff")["episodes"][0]["driver_move"] == pytest.approx(60)
    assert analytics.past_episodes(rates, -50, "diff")["n"] == 0

    # A market's response, including how it is compared with the sensitivity.
    episodes = [{"start": "2010-01-31", "end": "2010-02-28"}, {"start": "2010-02-28", "end": "2010-03-31"}, {"start": "2010-03-31", "end": "2010-04-30"},
                {"start": "2010-04-30", "end": "2010-05-31"}, {"start": "2010-05-31", "end": "2010-06-30"}]
    falling = _monthly([100, 90, 81, 72.9, 65.61, 59.049])
    res = analytics.episode_response(falling, "log_return", episodes, implied=3.0)
    assert res["n"] == 5 and res["median"] == pytest.approx(-10) and res["same_direction"] == 0 and res["disagrees"] is True
    agrees = analytics.episode_response(falling, "log_return", episodes, implied=-3.0)
    assert agrees["same_direction"] == 5 and agrees["disagrees"] is False
    few = analytics.episode_response(falling, "log_return", episodes[:2], implied=3.0)
    assert few == {"n": 2, "too_few": True}
    assert analytics.monthly_levels(_monthly(range(1, 6))) is None  # too short to be useful


def test_scan_shows_past_episodes_and_tests_a_portfolio(seeded):
    from atlas import auth, portfolios, scenarios
    from atlas.api.main import app
    from atlas.research import engine

    uid, ws = auth.create_user("port@scen.test", "correct horse battery", workspace_name="Portfolio tests")
    saved = portfolios.save("Softs book 2026", [{"dataset_id": "CMD.SUGAR.ISA", "weight": 0.6}, {"dataset_id": "CMD.CORN.US", "weight": -0.4}], ws, uid)
    other = portfolios.save("Not yours", [{"dataset_id": "CMD.SUGAR.ISA", "weight": 1}], "someone-else", "x")
    custom = [{"driver": "CMD.CRUDE.BRENT", "shock": 10, "name": "Oil +10%"}]

    res, _ = scenarios.scan(["CMD.COPPER.LME", "EQ.US.SP500"], custom, portfolio=saved)
    assert res["portfolio"]["name"] == "Softs book 2026" and res["portfolio"]["tested"] and res["portfolio"]["holdings"] == 2
    sc = res["scenarios"][0]
    hist = sc["history"]
    assert hist["n"] == len(hist["episodes"]) and hist["n"] > 0 and hist["threshold"] == 10
    assert all(e["driver_move"] >= 10 for e in hist["episodes"])
    assert all(a["end"] <= b["start"] for a, b in zip(hist["episodes"], hist["episodes"][1:]))  # windows never overlap
    for i in sc["impacts"]:
        if not i["is_driver"]:
            assert i["past"]["n"] <= hist["n"]
    pf = sc["portfolio"]
    assert pf["unit"] == "pct" and pf["ci95"][0] <= pf["implied_move"] <= pf["ci95"][1]
    assert pf["significant"] == (pf["p_value"] is not None and pf["p_value"] <= 0.05)
    # Each holding's contribution is its weight times its own sensitivity.
    res2, _ = scenarios.scan(["CMD.SUGAR.ISA"], custom)
    sugar = next(i for i in res2["scenarios"][0]["impacts"] if i["dataset_id"] == "CMD.SUGAR.ISA")
    top = next(c for c in pf["largest"] if c["dataset_id"] == "CMD.SUGAR.ISA")
    assert top["contribution"] == pytest.approx(0.6 * sugar["implied_move"], abs=0.01) and res2["portfolio"] is None

    # A portfolio that holds the driver itself counts the full shock for that holding.
    holds, _ = scenarios.scan(["EQ.US.SP500"], custom, portfolio={"name": "Oil", "holdings": [{"dataset_id": "CMD.CRUDE.BRENT", "weight": 0.5}]})
    assert holds["scenarios"][0]["portfolio"]["largest"][0]["contribution"] == 5.0

    # Through the report, with its figures verified; the portfolio name's digits are not treated as data.
    run_id = engine.create_run("Scenario outlook", {"engine": "deterministic", "template": "scenario_outlook", "formats": [], "scenario_portfolio": saved["portfolio_id"],
                                                    "scenario_universe": ["CMD.CRUDE.BRENT", "EQ.US.SP500"], "custom_scenarios": custom}, uid, ws)
    out = engine.execute_run(run_id)
    assert out["verification"]["passed"], out["verification"]["unverified_numbers"][:5]
    md = out["narrative_markdown"]
    assert "### Effect on Softs book 2026" in md and "*Past episodes:*" in md and "Past episodes (median)" in md

    # A portfolio deleted after a schedule was set up doesn't stop the scan.
    portfolios.delete(saved["portfolio_id"], ws)
    run_id = engine.create_run("Scenario outlook again", {"engine": "deterministic", "template": "scenario_outlook", "formats": [], "scenario_portfolio": saved["portfolio_id"],
                                                          "scenario_universe": ["CMD.CRUDE.BRENT", "EQ.US.SP500"]}, uid, ws)
    out = engine.execute_run(run_id)
    assert "no longer exists" in out["narrative_markdown"]

    # The API refuses another workspace's portfolio (ATLAS_AUTH=off: the local workspace).
    with TestClient(app) as c:
        r = c.post("/api/v1/research", json={"query": "Scenario outlook", "report": {"template": "scenario_outlook"}, "scenarios": {"portfolio_id": other["portfolio_id"]}})
        assert r.status_code == 422 and "portfolio" in r.json()["detail"]


def test_scan_options_are_part_of_the_cache_key():
    from atlas.research import engine

    base = {"template": "scenario_outlook"}
    keys = {engine.cache_key("Scenario outlook", {**base, **extra}) for extra in
            ({}, {"scenario_universe": ["EQ.US.SP500"]}, {"custom_scenarios": [{"driver": "FX.USDBRL", "shock": 10}]}, {"scenario_portfolio": "p1"})}
    assert len(keys) == 4
