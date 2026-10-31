from datetime import date, timedelta

from atlas.ingestion import quality
from atlas.provider_sdk import ParsedPoint
from atlas.research import verify
from atlas.research.planner import build_plan, validate_plan
from atlas.instruments import extract_entities, resolve


def _pts(values, start=date(2020, 1, 1)):
    return [ParsedPoint(start + timedelta(days=i), v) for i, v in enumerate(values)]


# ---------------------------------------------------------------- quality


def test_duplicates_and_missing_values_are_handled():
    pts = _pts([1.0, None, 3.0]) + [ParsedPoint(date(2020, 1, 1), 1.5)]
    clean, findings, metrics = quality.run_checks(pts, frequency="daily", allow_non_positive=False)
    assert [p.value for p in clean] == [1.5, 3.0]
    checks = {f.check for f in findings}
    assert {"duplicates", "missing_values"} <= checks
    assert not quality.blocking(findings)


def test_unit_change_blocks_publication():
    clean, findings, _ = quality.run_checks(_pts([100.0] * 40), frequency="daily", allow_non_positive=False,
                                            previous_values=[1.0] * 40)
    assert quality.blocking(findings)


def test_future_dates_block_publication():
    pts = [ParsedPoint(date.today() + timedelta(days=30), 1.0)]
    _, findings, _ = quality.run_checks(pts, frequency="daily", allow_non_positive=False)
    assert quality.blocking(findings)


def test_negative_prices_warn_but_do_not_block():
    _, findings, _ = quality.run_checks(_pts([20.0, -37.6, 10.0]), frequency="daily", allow_non_positive=False)
    assert any(f.check == "non_positive" and f.severity == "warning" for f in findings)
    assert not quality.blocking(findings)


# ---------------------------------------------------------------- verification

EVIDENCE = [
    {"label": "E1", "result": {"value": 14.812272, "change_pct": 6.4987, "unit": "US cents per pound"}},
    {"label": "E2", "result": {"pearson": -0.101, "trailing": {"1Y": {"return_pct": -9.49}}, "beta": 0.0123}},
]


def test_traceable_numbers_pass():
    text = "Sugar printed 14.81 [E1], up 6.50% [E1]. Correlation -0.101 and -9.49% over 1Y [E2]; beta 1.23% [E2]."
    res = verify.verify(text, EVIDENCE)
    assert res["passed"], res
    assert res["numbers_checked"] >= 4


def test_fabricated_number_is_flagged():
    res = verify.verify("Sugar printed 15.40 [E1].", EVIDENCE)
    assert not res["passed"]
    assert res["unverified_numbers"][0]["value"] == "15.40"


def test_invalid_citation_is_flagged():
    res = verify.verify("Sugar printed 14.81 [E9].", EVIDENCE)
    assert res["invalid_citations"] == ["E9"]
    assert not res["passed"]


def test_dates_horizons_and_ci_labels_are_not_treated_as_claims():
    text = "Over the 5-year window to 2026-07-01, a 95% CI and 12-month horizon in 2025 [E1]."
    assert verify.verify(text, EVIDENCE)["numbers_checked"] == 0


# ---------------------------------------------------------------- planner & resolution


def test_resolution_prefers_specific_keywords():
    assert resolve("Brazilian real")[0]["dataset_id"] == "FX.USDBRL"
    assert resolve("sugar")[0]["dataset_id"] == "CMD.SUGAR.ISA"
    assert "CMD.GOLD" in extract_entities("gold and copper")


def test_sugar_plan_includes_drivers_and_gap_tools():
    plan = build_plan("What's happening with sugar futures today, and why?")
    tools = [s["tool"] for s in plan["steps"]]
    assert plan["asset"]["dataset_id"] == "CMD.SUGAR.ISA"
    assert "calculate_correlation" in tools and "get_fundamentals" in tools
    assert not validate_plan(plan)


def test_rate_cut_scenario_plan():
    plan = build_plan("What happens to copper, gold, and emerging-market currencies if the Federal Reserve cuts interest rates?")
    scen = [s for s in plan["steps"] if s["tool"] == "run_scenario"]
    assert scen and all(s["args"]["driver"] == "RATE.UST.2Y" and s["args"]["shock"] < 0 for s in scen)
    assert not plan["coverage_gaps"] and "CMD.GOLD" in plan["entities"]
    assert {"FX.USDBRL", "FX.USDMXN"} <= set(plan["entities"])


def test_horizon_and_lookback_parsing():
    plan = build_plan("Relationship between the Brazilian real and sugar over the last ten years, outlook next six months")
    assert plan["timeframe"]["lookback_years"] == 10
    assert plan["timeframe"]["horizon_months"] == 6
    assert "relationship" in plan["intents"]


# ---------------------------------------------------------------- config


def test_dotenv_parsing_tolerates_common_formats():
    from atlas.config import parse_dotenv

    text = '﻿export ANTHROPIC_API_KEY="sk-ant-abc"\n# comment\nATLAS_LLM=auto  # inline\nEMPTY=\nQUOTED=\'x y\'\n'
    env = parse_dotenv(text)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-abc"
    assert env["ATLAS_LLM"] == "auto"
    assert env["EMPTY"] == ""
    assert env["QUOTED"] == "x y"


def test_significance_levels_are_not_claims_but_other_5s_are():
    ev = [{"label": "E1", "result": {"a": 1.25}}]
    assert verify.verify("No variable is significant at 5% [E1].", ev)["passed"]
    assert not verify.verify("Prices rose 5% [E1].", ev)["passed"]


def test_confidence_levels_are_not_claims_but_other_95s_are():
    assert verify.verify("VaR of 1.15% at 95% and 2.01% at 99% [E1].", [{"label": "E1", "result": {"a": 1.15, "b": 2.01}}])["passed"]
    assert not verify.verify("Inflation hit 95% [E1].", [{"label": "E1", "result": {"a": 1.15}}])["passed"]
