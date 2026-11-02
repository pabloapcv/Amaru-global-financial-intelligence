"""Scenario scan: which scenarios does the current data point to, and what would each do to other markets?

The scan reads the stored data and proposes scenarios from rules anyone can audit:

- an FOMC meeting in the next 60 days, a large move in the 2-year yield, or central banks worldwide
  leaning one way over 6 months → rate scenarios
- a large 3-month dollar move, or the dollar at an extreme of its 5-year range → dollar scenarios
- a large oil move or extreme → oil scenarios
- a calm volatility index → a volatility-spike scenario
- speculators at an extreme of their 3-year positioning range (CFTC) → a positioning-unwind scenario
- USDA stocks-to-use at an extreme of 20 years → a supply-squeeze or surplus scenario

plus any scenario the user asks for. Each scenario's effect on every market in the universe comes from
its historical sensitivity (analytics.scenario: HAC OLS of period changes over 10 years), with a 95%
range, and effects that are not statistically significant are marked as such. Next to each sensitivity
is what actually happened: the market's move in the past episodes when the driver moved that much
within 3 months, and how the sensitivity would have done in those episodes if fitted only on earlier
data. A saved portfolio can be tested too: its own monthly return series gets the same
treatment, so diversification between holdings is measured rather than assumed.

"Relevance" counts the data signals behind a scenario. It is not a probability, and the output says so.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta
from typing import Any

from atlas import analytics
from atlas.data.access import DataUnavailable, get_fundamentals, get_positioning, get_series
from atlas.instruments import fundamentals_for, get_dataset, positioning_for

log = logging.getLogger(__name__)

DEFAULT_UNIVERSE = ["CMD.CRUDE.BRENT", "CMD.COPPER.LME", "CMD.SUGAR.ISA", "CMD.CORN.US", "CMD.SOYBEANS.US", "CMD.COFFEE.ARABICA",
                    "FX.USD.BROAD", "FX.USDBRL", "FX.USDMXN", "RATE.UST.10Y", "EQ.US.SP500"]
RATES, DOLLAR, OIL, VOL = "RATE.UST.2Y", "FX.USD.BROAD", "CMD.CRUDE.BRENT", "VOL.US.VIX"
MAX_SCENARIOS = 8
MAX_UNIVERSE = 15
PORTFOLIO = "PORTFOLIO"
RECESSION_SIGNAL_PCT = 30.0
GLOBAL_LEAN_PCT = 25.0  # banks raising minus banks cutting, as a share of all banks
NOT_PROBABILITY = ("Relevance counts the data signals pointing to a scenario; it is not a probability. "
                   "Effects are historical sensitivities (all else equal), not forecasts.")


class ScenarioError(ValueError):
    pass


def _name(dsid: str) -> str:
    ds = get_dataset(dsid)
    return ds.name if ds else dsid


def _ordinal(x: float) -> str:
    n = int(round(x))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def _perf(series, dsid: str) -> dict[str, Any]:
    ds = get_dataset(dsid)
    assert ds is not None
    p = analytics.performance(series, unit=ds.unit, frequency=ds.frequency)
    t3 = p["trailing"].get("3M") or {}
    return {"value": p["last_value"], "date": p["last_date"], "pct_3m": t3.get("return_pct"), "bp_3m": t3.get("change_bp"),
            "percentile_5y": p.get("percentile_in_5y_range")}


def _scenario(sid: str, name: str, driver: str, shock: float, signals: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    ds = get_dataset(driver)
    assert ds is not None
    return {"id": sid, "name": name, "kind": kind, "driver": driver, "driver_name": ds.name, "shock": shock,
            "shock_unit": "bp" if analytics.transform_kind(ds.unit) != "log_return" else "pct",
            "signals": signals, "relevance": len(signals)}


def _test_summary(tests: list[dict[str, Any]]) -> dict[str, Any] | None:
    """One scenario's walk-forward test across its markets. Markets in the same episode are not independent."""
    scored = [t for t in tests if not t["too_few"]]
    if not scored:
        return None
    ratios = [t["error_ratio"] for t in scored if t["error_ratio"] is not None]
    moves, right = sum(t["n"] for t in scored), sum(t["right_way"] for t in scored)
    return {"episodes": max(t["n"] for t in scored), "markets": len(scored), "moves": moves, "right_way": right,
            "hit_pct": _r(100 * right / moves, 0), "error_ratio": _r(statistics.median(ratios), 2) if ratios else None,
            "reliable_moves": sum(t["reliable_n"] for t in scored), "reliable_right_way": sum(t["reliable_right_way"] for t in scored)}


def _portfolio_levels(weights: dict[str, float], levels: dict[str, Any]) -> Any:
    """Index of the holdings at fixed weights, or None if a period would wipe it out."""
    import pandas as pd

    returns = pd.concat({d: levels[d].pct_change(fill_method=None) for d in weights}, axis=1).dropna()
    if len(returns) < 24:
        return None
    combined = sum(returns[d] * w for d, w in weights.items())
    return None if (combined <= -1).any() else (1 + combined).cumprod()


def _portfolio_effect(sc: dict[str, Any], levels: Any, period_levels: Any, weights: dict[str, float], series: dict[str, Any],
                      freqs: dict[str, str], units: dict[str, str], episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    with_book, book_freqs, book_units = {**series, PORTFOLIO: period_levels}, {**freqs, PORTFOLIO: "monthly"}, {**units, PORTFOLIO: "index"}
    try:
        res = analytics.scenario(PORTFOLIO, sc["driver"], sc["shock"], with_book, book_freqs, book_units, lookback_years=10)
    except (ValueError, KeyError, StopIteration):
        return None
    contributions = []
    for dsid, weight in weights.items():
        if dsid == sc["driver"]:
            move = sc["shock"] if sc["shock_unit"] == "pct" else None
        else:
            try:
                move = analytics.scenario(dsid, sc["driver"], sc["shock"], series, freqs, units, lookback_years=10)["implied_asset_move"]
            except (ValueError, KeyError, StopIteration):
                move = None
        if move is not None:
            contributions.append({"dataset_id": dsid, "name": _name(dsid), "weight": weight, "contribution": _r(weight * move, 2)})
    p = res["beta_p_value"]
    return {"implied_move": res["implied_asset_move"], "unit": "pct", "ci95": res["implied_asset_move_ci95"], "p_value": _r(p, 4),
            "significant": p is not None and p <= 0.05,
            "past": analytics.episode_response(levels, "log_return", episodes, res["implied_asset_move"]),
            "tested": analytics.sensitivity_test(PORTFOLIO, sc["driver"], episodes, with_book, book_freqs, book_units, levels) if episodes else None,
            "largest": sorted(contributions, key=lambda c: -abs(c["contribution"]))[:3]}


def scan(universe: list[str] | None = None, custom: list[dict[str, Any]] | None = None,
         max_scenarios: int = MAX_SCENARIOS, portfolio: dict[str, Any] | None = None) -> tuple[dict[str, Any], list]:
    """Returns (result, metas used). Raises ScenarioError for bad input.

    `portfolio` is a saved portfolio (name and holdings) to test each scenario against.
    """
    universe = list(dict.fromkeys(universe or DEFAULT_UNIVERSE))
    if len(universe) > MAX_UNIVERSE:
        raise ScenarioError(f"at most {MAX_UNIVERSE} markets per scan")
    for dsid in universe + [c.get("driver", "") for c in custom or []]:
        ds = get_dataset(dsid)
        if ds is None or ds.kind not in ("market", "macro"):
            raise ScenarioError(f"unknown market or series: {dsid}")

    weights = {h["dataset_id"]: float(h["weight"]) for h in (portfolio or {}).get("holdings", [])}
    needed = list(dict.fromkeys(universe + [RATES, DOLLAR, OIL, VOL] + [c["driver"] for c in custom or []] + list(weights)))
    series, metas, perf = {}, [], {}
    for dsid in needed:
        try:
            s, m = get_series(dsid)
        except DataUnavailable:
            continue
        series[dsid] = s
        metas.append(m)
        try:
            perf[dsid] = _perf(s, dsid)
        except (ValueError, KeyError):
            pass

    candidates: list[dict[str, Any]] = []

    # Rates: the next FOMC meeting and the 2-year yield's recent direction.
    fomc = None
    try:
        from atlas import calendar

        fomc = next((e for e in calendar.upcoming(60)["events"] if e["kind"] == "fomc"), None)
    except Exception:  # the calendar is optional context
        log.debug("calendar unavailable for scenario scan", exc_info=True)
    fed_tone = None
    try:
        from atlas import tone

        fed_tone = tone.latest_reading("fed", 100)
    except Exception:  # tone is optional context
        log.debug("tone unavailable for scenario scan", exc_info=True)
    if fed_tone and not (fed_tone["compared_with"] and fed_tone["consistent"] and fed_tone["stance"]):
        fed_tone = None  # nothing to compare with, a flagged decision, or no change in tone
    from atlas import cycle

    recession = cycle.recession_signal()
    lean = None
    try:
        from atlas import policy_rates

        rates_now = policy_rates.overview()
        d = rates_now["direction"]
        if d["banks"] and abs(d["hiking"] - d["cutting"]) / d["banks"] * 100 >= GLOBAL_LEAN_PCT:
            lean = d
    except Exception:  # global rates are optional context
        log.debug("policy rates unavailable for scenario scan", exc_info=True)
    if RATES in perf:
        bp = perf[RATES]["bp_3m"]
        for direction, shock in (("down", -50.0), ("up", 50.0)):
            sig = []
            if fomc:
                sig.append({"text": f"FOMC meeting on {fomc['date']}", "date": fomc["date"]})
            if bp is not None and abs(bp) >= 25 and (bp < 0) == (direction == "down"):
                sig.append({"text": f"2-year Treasury yield moved {bp:+.0f} bp over 3 months", "value": bp, "unit": "bp"})
            if fed_tone and (fed_tone["stance"] < 0) == (direction == "down"):
                sig.append({"text": f"Fed statement of {fed_tone['meeting_date']} read as {fed_tone['stance_label']} ({fed_tone['stance']:+d} on a −2 to +2 scale; Claude's reading)",
                            "value": fed_tone["stance"], "unit": "stance", "url": fed_tone["url"]})
            if lean and (lean["cutting"] > lean["hiking"]) == (direction == "down"):
                sig.append({"text": f"Over {lean['months']} months, {lean['hiking']} of {lean['banks']} central banks raised rates and {lean['cutting']} cut",
                            "value": lean["hiking"] - lean["cutting"], "unit": "banks"})
                metas += [m for m in rates_now["sources"] if m not in metas]
            if direction == "down" and recession and recession["probability_pct"] >= RECESSION_SIGNAL_PCT:
                sig.append({"text": f"The recession model puts the 12-month chance at {recession['probability_pct']}%",
                            "value": recession["probability_pct"], "unit": "pct"})
            label = "Rates fall: 2-year Treasury yield −50 bp" if direction == "down" else "Rates rise: 2-year Treasury yield +50 bp"
            candidates.append(_scenario(f"rates_{direction}", label, RATES, shock, sig, "macro"))

    # Dollar and oil: momentum and stretch within the 5-year range.
    for dsid, size, big, label in ((DOLLAR, 5.0, 2.0, "Dollar"), (OIL, 20.0, 10.0, "Brent crude")):
        if dsid not in perf:
            continue
        pct, pctl = perf[dsid]["pct_3m"], perf[dsid]["percentile_5y"]
        for direction, shock in (("up", size), ("down", -size)):
            sig = []
            if pct is not None and abs(pct) >= big and (pct > 0) == (direction == "up"):
                sig.append({"text": f"{label} moved {pct:+.1f}% over 3 months", "value": pct, "unit": "pct"})
            if pctl is not None and ((direction == "down" and pctl >= 90) or (direction == "up" and pctl <= 10)):
                sig.append({"text": f"{label} sits at the {_ordinal(pctl)} percentile of its 5-year range", "value": pctl, "unit": "percentile"})
            candidates.append(_scenario(f"{label.split()[0].lower()}_{direction}", f"{label} {shock:+.0f}%", dsid, shock, sig, "macro"))

    # Volatility: calm markets are when a volatility spike would surprise most.
    if VOL in perf:
        pctl, pct = perf[VOL]["percentile_5y"], perf[VOL]["pct_3m"]
        sig = []
        if pctl is not None and pctl <= 20:
            sig.append({"text": f"Volatility index at the {_ordinal(pctl)} percentile of its 5-year range", "value": pctl, "unit": "percentile"})
        if pct is not None and pct >= 20:
            sig.append({"text": f"Volatility index up {pct:+.1f}% over 3 months", "value": pct, "unit": "pct"})
        candidates.append(_scenario("vol_spike", "Volatility spike: VIX +50%", VOL, 50.0, sig, "macro"))

    # Commodities in the universe: positioning extremes and USDA balances.
    start = date.today() - timedelta(days=4 * 365)
    for dsid in universe:
        ds = get_dataset(dsid)
        if ds is None or not dsid.startswith("CMD.") or dsid not in series:
            continue
        cot = positioning_for(dsid)
        if cot:
            try:
                df, m = get_positioning(cot, start=start)
                stats = analytics.positioning_stats(df, lookback_years=3)
                idx = stats.get("cot_index")
                if idx is not None and (idx >= 90 or idx <= 10):
                    metas.append(m)
                    long_ = idx >= 90
                    candidates.append(_scenario(
                        f"unwind_{dsid}", f"{ds.name} {'−10%' if long_ else '+10%'}: speculators {'sell' if long_ else 'buy back'}",
                        dsid, -10.0 if long_ else 10.0,
                        [{"text": f"Speculators' net position is at {idx:.0f} on a 0–100 scale of its 3-year range (CFTC, {stats['report_date']})",
                          "value": round(idx, 1), "unit": "index"}], "positioning"))
            except (DataUnavailable, ValueError, KeyError):
                pass
        fid = fundamentals_for(dsid)
        if fid:
            try:
                df, m = get_fundamentals(fid)
                fds = get_dataset(fid)
                bal = analytics.fundamentals_balance(df, unit=fds.unit if fds else "")
                stu = bal.get("stocks_to_use") or {}
                pc = stu.get("percentile")
                if pc is not None and (pc <= 20 or pc >= 80):
                    metas.append(m)
                    tight = pc <= 20
                    candidates.append(_scenario(
                        f"balance_{dsid}", f"{ds.name} {'+15%: supply squeeze' if tight else '−10%: surplus weighs'}", dsid,
                        15.0 if tight else -10.0,
                        [{"text": f"USDA world stocks-to-use is {stu['latest_pct']}% ({bal['latest_market_year']}), the {_ordinal(pc)} percentile of {stu['history_years']} years",
                          "value": stu["latest_pct"], "unit": "pct"}], "fundamentals"))
            except (DataUnavailable, ValueError, KeyError):
                pass

    for i, c in enumerate(custom or []):
        shock = float(c["shock"])
        if shock == 0:
            raise ScenarioError("a custom scenario needs a non-zero shock")
        name = (c.get("name") or "").strip()[:120] or f"{_name(c['driver'])} {shock:+g}"
        sc = _scenario(f"custom_{i + 1}", name, c["driver"], shock, [{"text": "Requested by you"}], "custom")
        sc["relevance"] = None  # chosen, not signalled
        candidates.append(sc)

    signalled = [c for c in candidates if c["kind"] == "custom" or c["relevance"]]
    if len([c for c in signalled if c["kind"] != "custom"]) < 2:  # quiet markets: keep the standard stress tests
        signalled += [c for c in candidates if c["id"] in ("rates_down", "rates_up", "dollar_up", "dollar_down") and c not in signalled]
    customs = [c for c in signalled if c["kind"] == "custom"]
    ranked = sorted((c for c in signalled if c["kind"] != "custom"), key=lambda c: (-c["relevance"], c["id"]))
    chosen = customs + ranked[:max(0, max_scenarios - len(customs))]

    freqs = {d: get_dataset(d).frequency for d in series}  # type: ignore[union-attr]
    units = {d: get_dataset(d).unit for d in series}  # type: ignore[union-attr]
    monthly = {d: m for d, s in series.items() if (m := analytics.monthly_levels(s)) is not None}
    held = {d: w for d, w in weights.items() if d in monthly}
    # Month-end levels for episodes; monthly averages, like every other market, for the sensitivity.
    portfolio_levels = _portfolio_levels(held, monthly) if held else None
    period_levels = None
    if portfolio_levels is not None:
        period_levels = _portfolio_levels(held, {d: series[d] if freqs[d] == "monthly" else analytics.resample(series[d], "monthly", how="mean") for d in held})


    for sc in chosen:
        driver_levels = monthly.get(sc["driver"])
        history = analytics.past_episodes(driver_levels, sc["shock"], analytics.transform_kind(units[sc["driver"]])) if driver_levels is not None else None
        sc["history"] = history
        episodes = history["episodes"] if history else []

        def past(dsid: str, implied: float) -> dict[str, Any] | None:
            if history is None or dsid not in monthly:
                return None
            return analytics.episode_response(monthly[dsid], analytics.transform_kind(units[dsid]), episodes, implied)

        impacts = []
        for asset in universe:
            if asset not in series:
                continue
            if asset == sc["driver"]:
                impacts.append({"dataset_id": asset, "name": _name(asset), "implied_move": sc["shock"], "unit": sc["shock_unit"],
                                "ci95": None, "p_value": None, "significant": True, "r_squared": None, "is_driver": True})
                continue
            try:
                res = analytics.scenario(asset, sc["driver"], sc["shock"], series, freqs, units, lookback_years=10)
            except (ValueError, KeyError, StopIteration):
                continue
            p = res["beta_p_value"]
            tested = analytics.sensitivity_test(asset, sc["driver"], episodes, series, freqs, units, monthly[asset]) if asset in monthly and episodes else None
            impacts.append({"dataset_id": asset, "name": _name(asset), "implied_move": res["implied_asset_move"],
                            "unit": res["implied_unit"], "ci95": res["implied_asset_move_ci95"], "p_value": _r(p, 4),
                            "significant": p is not None and p <= 0.05, "r_squared": res["r_squared"], "is_driver": False,
                            "past": past(asset, res["implied_asset_move"]), "tested": tested})
        reliable = [i for i in impacts if i["significant"] and not i["is_driver"]]
        sc["impacts"] = impacts
        sc["test"] = _test_summary([i["tested"] for i in impacts if i.get("tested")])
        # Effects in % and bp are not comparable by size; rank by how clearly the data shows them
        # (effect relative to the half-width of its 95% range).
        def clarity(i: dict[str, Any]) -> float:
            half = (i["ci95"][1] - i["ci95"][0]) / 2 if i["ci95"] else 0
            return abs(i["implied_move"]) / half if half else 0

        sc["clearest_effects"] = [i["dataset_id"] for i in sorted(reliable, key=lambda i: -clarity(i))[:3]]
        if sc["relevance"] is not None:
            sc["relevance_label"] = "strong" if sc["relevance"] >= 2 else "some" if sc["relevance"] == 1 else "standard stress test"
        if period_levels is not None:
            sc["portfolio"] = _portfolio_effect(sc, portfolio_levels, period_levels, held, series, freqs, units, episodes)

    as_of = max((perf[d]["date"] for d in universe if d in perf), default=None)
    result = {
        "as_of": as_of, "universe": [d for d in universe if d in series],
        "missing": [d for d in universe if d not in series],
        "considered": len(candidates), "scenarios": chosen,
        "latest": {d: {"value": perf[d]["value"], "date": perf[d]["date"], "change_3m_pct": perf[d]["pct_3m"],
                       "change_3m_bp": perf[d]["bp_3m"], "percentile_5y": perf[d]["percentile_5y"]}
                   for d in needed if d in perf},
        "next_fomc": fomc["date"] if fomc else None,
        "portfolio": {"id": portfolio.get("portfolio_id"), "name": portfolio["name"], "holdings": len(held),
                      "excluded": [_name(d) for d in weights if d not in held],
                      "tested": period_levels is not None} if portfolio else None,
        "method": {
            "selection": "rules on 3-month moves, 5-year percentiles, the FOMC calendar, the latest Fed statement's tone, the recession model (30% or more), central banks worldwide (raising minus cutting at least 25% of banks over 6 months), CFTC positioning (3-year COT index ≥90 or ≤10) "
                         "and USDA stocks-to-use (≤20th or ≥80th percentile of 20 years); custom scenarios always included",
            "effects": "historical sensitivity of each market to the scenario's driver: HAC OLS on period changes over 10 years; "
                       "95% range from the coefficient's standard error; 'significant' means p ≤ 0.05",
            "history": "past episodes are the non-overlapping 3-month windows, at month-ends, in which the driver moved at least the "
                       "scenario's shock, over up to 30 years; each market's move is measured over the same windows, and a median is shown "
                       "only with at least 3 episodes. Other things were also happening in those periods",
            "test": "for each past episode the sensitivity is refitted on the 10 years before the episode began and applied to the driver's actual move, "
                    "then compared with what the market did; 'error ratio' is its total error divided by that of assuming no move (below 1 is better), "
                    "the median across markets. Markets in one episode are not independent, so the episode count matters more than the number of moves; "
                    "history uses today's revised figures. A sensitivity needs 36 periods of history and is scored on at least 3 episodes",
            "portfolio": "the portfolio is treated as one monthly series at its current weights, so correlations between holdings are "
                         "included; 'largest' shows each holding's weight times its own sensitivity; "
                         "its past test also uses current weights, so it shows how this mix would have behaved, not how a real portfolio did",
            "caveat": NOT_PROBABILITY + " Sensitivities measure how markets moved together, not what causes what.",
        },
    }
    return result, metas


# ---------------------------------------------------------------- change since the last scan


def previous_scan(workspace_id: str) -> dict[str, Any] | None:
    """The workspace's most recent completed scan (from a Scenario Outlook run), with its run id and time."""
    from sqlalchemy import select

    from atlas.data.db import ResearchRun, session_scope

    with session_scope() as s:
        runs = s.execute(select(ResearchRun).where(ResearchRun.workspace_id == workspace_id, ResearchRun.status == "complete",
                                                   ResearchRun.cached_from.is_(None))
                         .order_by(ResearchRun.created_at.desc()).limit(50)).scalars()
        for r in runs:
            scan_ = next((e["result"] for e in (r.result or {}).get("evidence", []) if e.get("tool") == "scan_scenarios"), None)
            if scan_:
                return {"run_id": r.run_id, "at": r.created_at.isoformat(timespec="seconds") + "Z", "scan": scan_}
    return None


def compare(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any] | None:
    """What changed between two scans; marks new scenarios in `current`. Custom scenarios are left out (you chose them)."""
    if previous is None:
        return None
    # Only scenarios the data points to count; standard stress tests (0 signals) come and go with how quiet markets are.
    before = {s["id"]: s for s in previous["scan"]["scenarios"] if s["kind"] != "custom" and s["relevance"]}
    now = {s["id"]: s for s in current["scenarios"] if s["kind"] != "custom" and s["relevance"]}
    brief = lambda s: {"id": s["id"], "name": s["name"], "relevance": s["relevance"]}  # noqa: E731
    new = [brief(s) for k, s in now.items() if k not in before]
    for s in current["scenarios"]:
        s["is_new"] = s["id"] in now and s["id"] not in before
    stronger = [{"id": k, "name": s["name"], "from": before[k]["relevance"], "to": s["relevance"]}
                for k, s in now.items() if k in before and s["relevance"] > before[k]["relevance"]]
    weaker = [{"id": k, "name": s["name"], "from": before[k]["relevance"], "to": s["relevance"]}
              for k, s in now.items() if k in before and s["relevance"] < before[k]["relevance"]]
    return {"previous_run_id": previous["run_id"], "previous_scan_at": previous["at"], "previous_as_of": previous["scan"].get("as_of"),
            "new": new, "dropped": [brief(s) for k, s in before.items() if k not in now], "stronger": stronger, "weaker": weaker,
            "changed": bool(new or stronger)}


def change_summary(changes: dict[str, Any]) -> list[str]:
    out = [f"New: {x['name']} ({x['relevance']} signal{'s' if x['relevance'] != 1 else ''})" for x in changes["new"]]
    out += [f"Stronger: {x['name']} ({x['from']} → {x['to']} signals)" for x in changes["stronger"]]
    out += [f"No longer signalled: {x['name']}" for x in changes["dropped"]]
    return out


def notify_changes(run_id: str) -> bool:
    """After a Scenario Outlook run: webhook `scenarios.changed`, and email the creator of a scheduled scan."""
    import os

    from atlas import mailer, webhooks
    from atlas.data.db import ReportSchedule, ResearchRun, User, session_scope

    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None or run.status != "complete" or run.cached_from:
            return False
        scan_ = next((e["result"] for e in (run.result or {}).get("evidence", []) if e.get("tool") == "scan_scenarios"), None)
        changes = (scan_ or {}).get("changes")
        if not changes or not changes.get("changed"):
            return False
        ws, schedule_id = run.workspace_id, (run.config or {}).get("schedule_id")
        sched = s.get(ReportSchedule, schedule_id) if schedule_id else None
        user = s.get(User, sched.user_id) if sched else None
        to = user.email if user else None
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    summary = change_summary(changes)
    webhooks.emit(ws, "scenarios.changed", {"run_id": run_id, "summary": summary, "new": changes["new"], "stronger": changes["stronger"],
                                            "dropped": changes["dropped"], "research_url": f"{base}/research?run={run_id}"})
    if to:
        try:
            mailer.send(to, "Amaru scenario agent: new scenarios", "Since the last scan:\n\n" + "\n".join(f"- {m}" for m in summary)
                        + f"\n\nSee the scenarios: {base}/scenarios\n\n{NOT_PROBABILITY}\n")
        except Exception:
            log.exception("could not email scenario changes for %s", run_id)
    return True
