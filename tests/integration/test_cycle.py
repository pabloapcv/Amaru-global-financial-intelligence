"""Business cycle: the recession model's discipline and record, and similar periods."""

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient


def synthetic(months: int = 360, seed: int = 7) -> dict[str, pd.Series]:
    """Recessions every 8 years or so, preceded by an inverted curve and tight conditions."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("1985-01-01", periods=months, freq="MS")
    rec = np.zeros(months)
    for start in range(60, months - 20, 96):
        rec[start:start + 10] = 1
    soon = pd.Series(rec, index=idx)[::-1].rolling(12, min_periods=1).max()[::-1].shift(-1).fillna(0).to_numpy()
    curve = 2 - 2.5 * soon + rng.normal(0, 0.4, months)
    nfci = -0.5 + 1.0 * soon + rng.normal(0, 0.2, months)
    unemployment = 5 + np.cumsum(rec * 0.25 - (1 - rec) * 0.03) + rng.normal(0, 0.03, months)
    level = lambda g: pd.Series(100 * np.exp(np.cumsum(g + rng.normal(0, 0.002, months))), index=idx)  # noqa: E731
    return {"curve": pd.Series(curve, index=idx), "nfci": pd.Series(nfci, index=idx), "unemployment": pd.Series(unemployment, index=idx),
            "recession": pd.Series(rec, index=idx), "payrolls": level(0.001 - rec * 0.003), "production": level(0.002 - rec * 0.006),
            "core_cpi": level(0.002 + 0 * rec)}


def test_labels_and_scoring():
    from atlas import cycle

    idx = pd.date_range("2000-01-01", periods=6, freq="MS")
    label = cycle.forward_label(pd.Series([0, 0, 0, 1, 0, 0.0], index=idx), horizon=2)
    assert label.tolist()[:4] == [0, 1, 1, 0] and label.iloc[4:].isna().all()  # unknown until both months are known
    assert cycle.auc(np.array([0.9, 0.8, 0.1, 0.2]), np.array([1, 1, 0, 0])) == 1
    assert cycle.auc(np.array([0.1, 0.2, 0.9, 0.8]), np.array([1, 1, 0, 0])) == 0
    assert cycle.auc(np.array([0.5, 0.5, 0.5, 0.5]), np.array([1, 1, 0, 0])) == 0.5
    assert cycle.auc(np.array([0.5, 0.5]), np.array([0, 0])) is None


def test_a_missing_month_doesnt_blank_the_following_year():
    from atlas import cycle

    raw = synthetic()
    raw["unemployment"] = raw["unemployment"].drop(raw["unemployment"].index[-8])
    assert cycle.inputs(raw).index[-1] == raw["curve"].index[-1]


def test_no_future_information_reaches_an_estimate():
    from atlas import cycle

    raw = synthetic()
    x = cycle.inputs(raw)
    month = x.index[300]
    label = cycle.forward_label(raw["recession"])
    params, n, _ = cycle.fit(x, label, cycle.INPUTS, month)

    # Rewriting everything the model isn't allowed to know leaves the estimate exactly the same.
    changed = raw["recession"].copy()
    changed[changed.index > month - pd.DateOffset(months=cycle.LABEL_LAG)] = 1
    future = x.copy()
    future.loc[future.index > month - pd.DateOffset(months=cycle.HORIZON + cycle.LABEL_LAG)] = 9.0
    again, n2, _ = cycle.fit(future, cycle.forward_label(changed), cycle.INPUTS, month)
    assert n == n2 and np.allclose(params.values, again.values)


def test_record_compares_with_the_same_training_average():
    from atlas import cycle

    raw = synthetic()
    res = cycle.recession_outlook(raw)
    assert 0 <= res["probability_pct"] <= 100 and res["as_of"] == raw["curve"].index[-1].date().isoformat()
    rec = res["record"]
    assert 0 <= rec["months_high_no_recession"] <= rec["months_high"] <= rec["months_scored"]
    assert rec["auc"] > 0.8 and rec["recessions_covered"] >= 2 and rec["beats_average"] == (rec["brier"] < rec["brier_average_only"])
    assert rec["last_month"] < res["as_of"]  # months whose outcome isn't known aren't scored
    assert res["simple_model"]["inputs"] == cycle.SIMPLE and len(res["path"]) >= rec["months_scored"]

    # An older history full of recessions must not make the benchmark easier to beat.
    extra = pd.Series(1.0, index=pd.date_range("1900-01-01", raw["recession"].index[0] - pd.DateOffset(months=1), freq="MS"))
    longer = cycle.recession_outlook({**raw, "recession": pd.concat([extra, raw["recession"]])})
    assert longer["record"]["brier_average_only"] == rec["brier_average_only"]


def test_similar_periods_are_spread_out_and_not_recent():
    from atlas import cycle

    x = cycle.state(synthetic())
    found = cycle.nearest(x, k=5)
    assert len(found) == 5 and [d for _, d in found] == sorted(d for _, d in found)
    months = [m for m, _ in found]
    assert all(abs((a.year - b.year) * 12 + a.month - b.month) >= cycle.MIN_GAP for i, a in enumerate(months) for b in months[:i])
    assert max(months) <= x.index[-1] - pd.DateOffset(months=cycle.EXCLUDE_RECENT)
    with_self = x.copy()
    with_self.iloc[100] = x.iloc[-1]  # an identical past month is the closest
    assert cycle.nearest(with_self)[0][0] == x.index[100]


def test_cycle_reaches_the_api_reports_and_scenarios(seeded, monkeypatch):
    from atlas import auth, cycle, scenarios
    from atlas.api.main import app
    from atlas.research import engine

    with TestClient(app) as c:  # not ingested yet
        body = c.get("/api/v1/macro/cycle").json()
        assert body["available"] is False and body["reason"]  # the seed data has too little for it
    assert cycle.recession_signal() is None

    monkeypatch.setattr(cycle, "_series", synthetic)
    cycle._cache.clear()
    with TestClient(app) as c:
        body = c.get("/api/v1/macro/cycle").json()
        assert body["available"] and body["recession"]["path"] and len(body["similar"]["periods"]) == 5 and "very few recessions" in body["caveat"]

    uid, ws = auth.create_user("cycle@scen.test", "correct horse battery", workspace_name="Cycle")
    out = engine.execute_run(engine.create_run("Scenario outlook cycle", {"engine": "deterministic", "template": "scenario_outlook", "formats": [],
                                                                          "scenario_universe": ["CMD.CRUDE.BRENT", "EQ.US.SP500"]}, uid, ws))
    assert out["verification"]["passed"], out["verification"]["unverified_numbers"][:5]
    md = out["narrative_markdown"]
    assert "## Business cycle" in md and "### Similar periods" in md and "for always guessing the historical average" in md
    ev = next(e for e in out["evidence"] if e["tool"] == "get_business_cycle")
    assert "path" not in ev["result"]["recession"]  # the chart carries it

    high = {"probability_pct": 42.0, "record": {"beats_average": True}}
    monkeypatch.setattr(cycle, "recession_signal", lambda: high)
    scan, _ = scenarios.scan(["CMD.CRUDE.BRENT"], max_scenarios=8)
    down = next(s for s in scan["scenarios"] if s["id"] == "rates_down")
    assert any("42.0%" in g["text"] for g in down["signals"])
    monkeypatch.setattr(cycle, "recession_signal", lambda: {**high, "probability_pct": 12.0})
    scan, _ = scenarios.scan(["CMD.CRUDE.BRENT"], max_scenarios=8)
    assert not any("recession model" in g["text"] for s in scan["scenarios"] for g in s["signals"])
    cycle._cache.clear()
