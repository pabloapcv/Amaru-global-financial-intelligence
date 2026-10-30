"""Report templates (spec §12). A template adds the investigation steps its report type needs
and tells the writer (Claude or deterministic) what the report is for. Every number still comes
from the same tools and passes the same verification.
"""

from __future__ import annotations

from typing import Any, Callable

KEY_MARKETS = ["CMD.CRUDE.BRENT", "CMD.COPPER.LME", "FX.USD.BROAD", "RATE.UST.10Y", "EQ.US.SP500", "VOL.US.VIX"]


def _daily(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    steps: list[tuple[str, dict[str, Any]]] = [("get_data_status", {"dataset_ids": KEY_MARKETS})]
    for ac in ("commodity", "fx", "rates", "equity"):
        steps.append(("compare_performance", {"asset_class": ac}))
    steps.append(("screen_anomalies", {}))
    steps += [("detect_regime", {"dataset_id": d}) for d in ("CMD.CRUDE.BRENT", "FX.USD.BROAD", "EQ.US.SP500")]
    steps.append(("get_economic_calendar", {"days": 14}))
    steps.append(("search_documents", {"query": "", "days": 7}))
    return steps


def _commodity(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    from atlas.instruments import get_dataset, positioning_for

    asset = (plan.get("asset") or {}).get("dataset_id")
    if not asset or (get_dataset(asset) and get_dataset(asset).meta.get("asset_class") != "commodity"):  # type: ignore[union-attr]
        asset = "CMD.CRUDE.BRENT"
    steps: list[tuple[str, dict[str, Any]]] = [
        ("get_latest_quote", {"dataset_id": asset}),
        ("get_price_history", {"dataset_id": asset, "lookback_years": 10}),
        ("get_seasonality", {"dataset_id": asset}),
        ("detect_regime", {"dataset_id": asset}),
        ("run_forecast", {"dataset_id": asset, "horizon_months": 6}),
        ("get_fundamentals", {"commodity": get_dataset(asset).name}),  # type: ignore[union-attr]
        ("get_futures_curve", {"underlying": asset}),
    ]
    if positioning_for(asset):
        steps.append(("get_positioning", {"dataset_id": positioning_for(asset)}))
    return steps


def _macro(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    steps: list[tuple[str, dict[str, Any]]] = [
        ("compare_countries", {"group": "g20", "indicators": ["GDP_GROWTH", "INFLATION", "UNEMPLOYMENT", "CURRENT_ACCOUNT", "DEBT"]}),
        ("compare_countries", {"group": "latam", "indicators": ["GDP_GROWTH", "INFLATION", "UNEMPLOYMENT"]}),
    ]
    steps += [("get_macro_series", {"series_id": s, "lookback_years": 5}) for s in ("US.GDP.REAL", "US.CPI.HEADLINE", "US.CPI.CORE", "US.UNRATE")]
    steps.append(("compare_performance", {"dataset_ids": ["RATE.US.FEDFUNDS", "RATE.UST.2Y", "RATE.UST.10Y", "RATE.UST.10Y.REAL", "FX.USD.BROAD", "EQ.US.SP500"]}))
    steps += [("detect_regime", {"dataset_id": d}) for d in ("FX.USD.BROAD", "RATE.UST.10Y")]
    steps.append(("get_economic_calendar", {"days": 60}))
    return steps


def _country(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    from atlas.instruments import countries, trade_pairs

    iso = (plan.get("countries") or ["BRA"])[0]
    groups = next((c["groups"] for c in countries() if c["iso3"] == iso), ["latam"])
    peers_group = "latam" if "latam" in groups else "g20"
    steps: list[tuple[str, dict[str, Any]]] = [
        ("get_country_profile", {"country": iso}),
        ("compare_countries", {"group": peers_group, "indicators": ["GDP_GROWTH", "INFLATION", "UNEMPLOYMENT", "CURRENT_ACCOUNT", "RESERVES"]}),
    ]
    steps += [("get_trade_flows", {"reporter": a, "partner": b}) for a, b in trade_pairs() if a == iso][:3]
    return steps


def _cross_asset(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("compare_performance", {"dataset_ids": KEY_MARKETS + ["CMD.SUGAR.ISA", "FX.USDBRL"]}),
        ("calculate_correlation", {"dataset_ids": ["CMD.CRUDE.BRENT", "CMD.COPPER.LME", "FX.USD.BROAD", "EQ.US.SP500"], "lookback_years": 5}),
        ("run_regression", {"dependent": "CMD.COPPER.LME", "explanatory": ["FX.USD.BROAD", "EQ.US.SP500", "RATE.UST.10Y"], "lookback_years": 10}),
        ("screen_anomalies", {}),
    ]


def _scenarios(plan: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    inputs = plan.get("scenario_inputs") or {}
    scan: dict[str, Any] = {}
    if inputs.get("universe"):
        scan["universe"] = inputs["universe"]
    if inputs.get("custom_scenarios"):
        scan["custom_scenarios"] = inputs["custom_scenarios"]
    if inputs.get("portfolio_id"):
        scan["portfolio_id"] = inputs["portfolio_id"]
    return [("scan_scenarios", scan), ("get_economic_calendar", {"days": 60}), ("screen_anomalies", {})]


TEMPLATES: dict[str, dict[str, Any]] = {
    "research_report": {
        "name": "Research report",
        "description": "General question-driven research.",
        "default_query": None, "depth": None, "steps": None,
        "brief": None,
    },
    "daily_market_brief": {
        "name": "Daily Market Brief",
        "description": "Latest moves across commodities, FX, rates and equities; unusual moves; volatility regimes; catalysts.",
        "default_query": "Daily market brief: what moved across commodities, currencies, rates and equities, and what stands out?",
        "depth": "standard", "steps": _daily,
        "brief": "Write a concise daily market brief: biggest movers by asset class, anything statistically unusual, "
                 "current volatility regimes, and what to watch. Name only scheduled events listed by the economic calendar tool, with their ISO dates.",
    },
    "commodity_deep_dive": {
        "name": "Commodity Deep Dive",
        "description": "One commodity in depth: price history, drivers, positioning, seasonality, regime, forecast, data gaps.",
        "default_query": "Commodity deep dive: crude oil.",
        "depth": "deep", "steps": _commodity,
        "brief": "Write an institutional commodity deep dive: market snapshot, 10-year history, drivers and relationships, "
                 "speculative positioning, seasonality, volatility regime, statistical outlook versus baselines, risks, and fundamentals gaps.",
    },
    "global_macro_outlook": {
        "name": "Global Macro Outlook",
        "description": "Growth, inflation and labor across the G20 and Latin America; U.S. detail; rates, dollar and regimes.",
        "default_query": "Global macro outlook: growth, inflation, rates and the dollar.",
        "depth": "standard", "steps": _macro,
        "brief": "Write a global macro outlook: regional growth and inflation comparison (flag stale country data), U.S. detail, "
                 "rates and dollar, volatility regimes, and key risks. Annual World Bank data lags; say which years you compare.",
    },
    "country_intelligence": {
        "name": "Country Intelligence Report",
        "description": "One country's economic profile, peer comparison and trade relationships.",
        "default_query": "Country intelligence report: Brazil.",
        "depth": "standard", "steps": _country,
        "brief": "Write a country intelligence report: economic profile, how it compares with regional peers, external position "
                 "and reserves, main trade relationships and product concentration, and financial risks.",
    },
    "scenario_outlook": {
        "name": "Scenario Outlook",
        "description": "Scenarios the current data points to (rates, dollar, oil, volatility, positioning, USDA balances, plus your own) and what each would mean for other markets.",
        "default_query": "Scenario outlook: which scenarios does the current data point to, and what would each mean across markets?",
        "depth": "standard", "steps": _scenarios,
        "brief": "Write a scenario outlook from the scan_scenarios evidence. Rank scenarios by relevance and, for each: why the data puts it "
                 "on the list (cite its signals), how it would carry across markets using only statistically significant sensitivities "
                 "(say when an effect is not significant, and that sensitivities describe how markets moved together, not causes), and "
                 "which scheduled events from the economic calendar would confirm or rule it out. Compare each sensitivity with what happened in "
                 "the past episodes, and say so when they disagree or when there are too few episodes to judge. If the scan tested a portfolio, "
                 "say which scenarios would hurt it most and which holdings matter. Never state or imply a probability: "
                 "relevance counts data signals. If the scan reports changes since the last scan, lead with them. Close with what to watch.",
    },
    "cross_asset": {
        "name": "Cross-Asset Research",
        "description": "Relationships among commodities, currencies, rates and equities.",
        "default_query": "Cross-asset research: how are commodities, the dollar, rates and equities moving together?",
        "depth": "standard", "steps": _cross_asset,
        "brief": "Write cross-asset research: relative performance, correlation structure and its stability, sensitivities, "
                 "and unusual moves. Treat correlations as associations, not causes.",
    },
}


def _subject(args: dict[str, Any]) -> str | None:
    return args.get("dataset_id") or args.get("series_id") or args.get("country")


def list_templates() -> list[dict[str, Any]]:
    return [{"key": k, "name": t["name"], "description": t["description"], "default_query": t["default_query"]}
            for k, t in TEMPLATES.items()]


def apply(plan: dict[str, Any], template: str | None) -> dict[str, Any]:
    """Merge a template's required steps into a plan (deduplicated, template steps first)."""
    t = TEMPLATES.get(template or "research_report")
    if t is None:
        raise ValueError(f"unknown template '{template}'")
    plan["template"] = template or "research_report"
    plan["template_brief"] = t["brief"]
    builder: Callable | None = t["steps"]
    if builder:
        extra = [{"tool": tool, "args": args} for tool, args in builder(plan)]
        # A template step supersedes a plan step with the same tool on the same dataset (e.g. a 10-year
        # history replaces the 5-year one), so reports don't repeat the same analysis twice.
        taken = {(e["tool"], _subject(e["args"])) for e in extra if _subject(e["args"])}
        rest = [s for s in plan["steps"] if s not in extra and (s["tool"], _subject(s["args"])) not in taken]
        plan["steps"] = extra + rest
    return plan
