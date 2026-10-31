"""Intent parsing, entity resolution and deterministic research plans (spec §8.1, §8.3).

The plan is validated against the registry before execution. With an LLM configured, the plan
is given to the model as a starting point it may extend; without one, the plan is executed
as-is and the deterministic writer composes the report.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from atlas.instruments import (
    country_indicators,
    datasets,
    extract_countries,
    extract_entities,
    get_dataset,
    positioning_for,
    trade_pairs,
)

# Known cross-asset drivers for flagship workflows (spec §9, §21).
DRIVERS: dict[str, list[str]] = {
    "CMD.SUGAR.ISA": ["FX.USDBRL", "CMD.CRUDE.BRENT", "FX.USDINR", "FX.USD.BROAD"],
    "CMD.COPPER.LME": ["FX.USDCNY", "FX.USD.BROAD", "EQ.US.SP500", "RATE.UST.10Y"],
    "CMD.COFFEE.ARABICA": ["FX.USDBRL", "FX.USD.BROAD"],
    "CMD.COCOA.ICCO": ["FX.USD.BROAD", "EQ.US.SP500"],
    "CMD.WHEAT.USHRW": ["CMD.CORN.US", "FX.USD.BROAD", "CMD.CRUDE.BRENT"],
    "CMD.CORN.US": ["CMD.CRUDE.WTI", "CMD.SOYBEANS.US", "FX.USD.BROAD"],
    "CMD.SOYBEANS.US": ["FX.USDBRL", "FX.USDCNY", "FX.USD.BROAD"],
    "CMD.CRUDE.WTI": ["FX.USD.BROAD", "EQ.US.SP500", "RATE.US.BREAKEVEN10Y"],
    "CMD.CRUDE.BRENT": ["FX.USD.BROAD", "EQ.US.SP500", "RATE.US.BREAKEVEN10Y"],
    "CMD.NATGAS.HH": ["CMD.CRUDE.WTI"],
    "CMD.GOLD": ["RATE.UST.10Y.REAL", "FX.USD.BROAD"],
    "CMD.SILVER": ["RATE.UST.10Y.REAL", "FX.USD.BROAD", "CMD.COPPER.LME"],
    "FX.USDBRL": ["FX.USD.BROAD", "RATE.UST.10Y", "VOL.US.VIX"],
    "FX.USDMXN": ["FX.USD.BROAD", "RATE.UST.10Y", "VOL.US.VIX"],
}
EM_FX = ["FX.USDBRL", "FX.USDMXN", "FX.USDINR"]
MACRO_DASHBOARD = ["US.GDP.REAL", "US.CPI.HEADLINE", "US.CPI.CORE", "US.UNRATE", "US.PAYEMS", "US.INDPRO"]
MACRO_MARKETS = ["RATE.US.FEDFUNDS", "RATE.UST.2Y", "RATE.UST.10Y", "FX.USD.BROAD", "EQ.US.SP500", "VOL.US.VIX"]

INTENT_PATTERNS = [
    ("anomaly_screen", r"\b(unusual|anomal|outlier|abnormal|spike|screen)"),
    ("scenario", r"\b(what happens|if the|scenario|shock|cuts? (interest )?rates?|hikes?|rate cut|rate hike)"),
    ("relationship", r"\b(relationship|correlat|regress|sensitiv|between|vs\.?|versus|beta|linked)"),
    ("forecast", r"\b(forecast|outlook|next (\d+|three|six|twelve) months?|predict|expect)"),
    ("comparison", r"\b(compare|rank|which|best|worst|across)"),
    ("macro_overview", r"\b(global economy|economy|macro|inflation|unemployment|gdp|growth|recession)"),
    ("trade", r"\b(trade|trading|exports?|imports?|exporters?|importers?|bilateral)\b"),
    ("market_update", r"\b(today|happening|latest|now|why is|moving|falling|rising|drop|rally)"),
]

WORD_NUM = {"one": 1, "two": 2, "three": 3, "six": 6, "twelve": 12, "five": 5, "ten": 10, "twenty": 20}


def _horizon_months(q: str) -> int:
    m = re.search(r"next (\d+|one|two|three|six|twelve) (months?|quarters?|years?)", q)
    if not m:
        return 3
    n = int(m.group(1)) if m.group(1).isdigit() else WORD_NUM[m.group(1)]
    unit = m.group(2)
    return min(24, n * (3 if unit.startswith("quarter") else 12 if unit.startswith("year") else 1))


def _lookback_years(q: str) -> float:
    m = re.search(r"(?:last|past|over(?: the)?(?: last| past)?) (\d+|one|two|three|five|ten|twenty) years?", q)
    if m:
        return float(m.group(1)) if m.group(1).isdigit() else float(WORD_NUM[m.group(1)])
    m = re.search(r"\b(\d+)\s*-?\s*(?:y|yrs?|years?)\b", q)
    return float(m.group(1)) if m else 5.0


def _shock(q: str) -> tuple[str, float] | None:
    m = re.search(r"(\d+)\s*(?:bp|basis points?)", q)
    bp = float(m.group(1)) if m else 50.0
    if re.search(r"\bcuts?\b|\blower", q):
        return "RATE.UST.2Y", -bp
    if re.search(r"\bhikes?\b|\braises?\b", q):
        return "RATE.UST.2Y", bp
    m = re.search(r"(?:dollar|usd)[^.]*?(strengthen|stronger|rall|weaken|weaker|fall)", q)
    if m:
        return "FX.USD.BROAD", 5.0 if m.group(1).startswith(("strength", "strong", "rall")) else -5.0
    m = re.search(r"oil[^.]*?(\d+)\s*%", q)
    if m:
        return "CMD.CRUDE.BRENT", float(m.group(1)) * (-1 if re.search(r"oil[^.]*(fall|drop|declin)", q) else 1)
    return None


def build_plan(query: str, *, depth: str = "standard", horizon_months: int | None = None,
               lookback_years: float | None = None, scope: list[str] | None = None) -> dict[str, Any]:
    q = query.lower()
    intents = [name for name, pat in INTENT_PATTERNS if re.search(pat, q)] or ["market_update"]
    entities = extract_entities(query)
    for extra in scope or []:
        if get_dataset(extra) and extra not in entities:
            entities.append(extra)
    horizon = horizon_months or _horizon_months(q)
    lookback = lookback_years or _lookback_years(q)

    country_codes = extract_countries(query)
    if re.search(r"global economy|world economy|across the world", q) and not country_codes:
        country_codes = [c for c in extract_countries("g20")]
    positioning_entities = [e for e in entities if get_dataset(e) and get_dataset(e).kind == "positioning"]  # type: ignore[union-attr]
    entities = [e for e in entities if e not in positioning_entities]
    if re.search(r"positioning|speculat|\bcot\b|commitments of traders|crowded", q):
        intents.append("positioning")

    if not entities and "macro_overview" in intents and not country_codes:
        entities = list(MACRO_DASHBOARD)
    if not entities and not country_codes and not positioning_entities and "anomaly_screen" not in intents and "comparison" not in intents:
        entities = MACRO_DASHBOARD[:3] + MACRO_MARKETS[:3]
        intents.append("macro_overview")

    if re.search(r"emerging[- ]market|\bem (fx|currenc)|latam currenc", q):
        entities += [e for e in EM_FX if e not in entities]
    if "scenario" in intents and _shock(q) and _shock(q)[0].startswith("RATE."):  # type: ignore[index]
        # Rates are the shocked driver here, not an asset to analyse.
        entities = [e for e in entities if not e.startswith("RATE.")]

    primary = [e for e in entities if get_dataset(e) and get_dataset(e).kind == "market"][:5]  # type: ignore[union-attr]
    macro = [e for e in entities if get_dataset(e) and get_dataset(e).kind == "macro"]  # type: ignore[union-attr]
    if country_codes and "USA" not in country_codes:
        # Keyword hits like "inflation" resolve to U.S. series; drop them when the question is about other countries.
        macro = [e for e in macro if not e.startswith("US.")]
        entities = [e for e in entities if not e.startswith("US.")]
    steps: list[dict[str, Any]] = []
    gaps: list[str] = []

    def add(tool: str, **args: Any) -> None:
        step = {"tool": tool, "args": args}
        if step not in steps:
            steps.append(step)

    if entities:
        add("get_data_status", dataset_ids=entities)
    for e in primary:
        ds = get_dataset(e)
        assert ds is not None
        if depth != "quick" and positioning_for(e):
            add("get_positioning", dataset_id=positioning_for(e))
        if not ds.has_source:
            gaps.append(e)
            continue
        add("get_latest_quote", dataset_id=e)
        add("get_price_history", dataset_id=e, lookback_years=lookback)
        if depth != "quick":
            add("detect_anomalies", dataset_id=e)
    for e in positioning_entities:
        add("get_positioning", dataset_id=e)
    for e in macro:
        add("get_macro_series", series_id=e, lookback_years=lookback)
    if country_codes:
        wanted = [i["key"] for i in country_indicators() if any(k in q for k in i.get("keywords", []))]
        if re.search(r"vulnerab|stronger dollar|external|crisis", q):
            wanted += ["CURRENT_ACCOUNT", "RESERVES", "DEBT", "INFLATION"]
        indicators = list(dict.fromkeys(wanted)) or ["GDP_GROWTH", "INFLATION", "UNEMPLOYMENT"]
        if "trade" in intents:
            loaded = set(trade_pairs())
            pairs = [(a, b) for a in country_codes for b in country_codes if a != b and (a, b) in loaded]
            pairs += [(a, "WLD") for a in country_codes if (a, "WLD") in loaded]
            for rep, par in pairs[:4]:
                add("get_trade_flows", reporter=rep, partner=par)
            if not pairs:
                add("get_trade_flows", reporter=country_codes[0], partner=country_codes[1] if len(country_codes) > 1 else "WLD")
        if len(country_codes) == 1 and not primary:
            add("get_country_profile", country=country_codes[0])
        else:
            add("compare_countries", countries=country_codes[:20], indicators=indicators[:5])
    if re.search(r"policy rate|central bank|rate (cut|hike|decision)|interest rates? (around|across|in )|monetary policy|selic|banxico", q):
        add("get_policy_rates", **({"countries": country_codes[:20]} if country_codes else {}))
    us_focus = not country_codes or "USA" in country_codes
    if "macro_overview" in intents and us_focus:
        for e in MACRO_DASHBOARD:
            add("get_macro_series", series_id=e, lookback_years=lookback)
    if "macro_overview" in intents and us_focus:
        add("compare_performance", dataset_ids=MACRO_MARKETS)

    sourced = [e for e in primary if get_dataset(e).has_source]  # type: ignore[union-attr]
    if depth != "quick":
        for e in sourced[:2]:
            drivers = [d for d in DRIVERS.get(e, []) if get_dataset(d) and get_dataset(d).has_source and d not in sourced]  # type: ignore[union-attr]
            if drivers:
                add("calculate_correlation", dataset_ids=[e, *drivers[:3]], lookback_years=lookback)
                add("run_regression", dependent=e, explanatory=drivers[:3], lookback_years=max(lookback, 5))
    if "relationship" in intents and len(sourced) >= 2:
        add("calculate_correlation", dataset_ids=sourced[:4], lookback_years=lookback)
        add("run_regression", dependent=sourced[0], explanatory=sourced[1:4], lookback_years=lookback)
    if ("forecast" in intents or depth == "deep") and sourced:
        for e in sourced[:2]:
            if depth == "deep":
                add("run_forecast", dataset_id=e, horizon_months=horizon, include_ml=True)
            else:
                add("run_forecast", dataset_id=e, horizon_months=horizon)
    if depth == "deep" or re.search(r"regime|volatil|turbulen|calm|risk", q):
        for e in sourced[:3]:
            add("detect_regime", dataset_id=e)
    if depth == "deep":
        for e in sourced[:2]:
            add("get_seasonality", dataset_id=e)
    if "scenario" in intents:
        shock = _shock(q)
        if shock:
            driver, size = shock
            targets = [e for e in sourced if e != driver] or ["CMD.COPPER.LME", "FX.USDBRL", "EQ.US.SP500"]
            for e in targets[:5]:
                add("run_scenario", asset=e, driver=driver, shock=size, lookback_years=10)
            if driver.startswith("RATE."):
                etype = "fed_cut" if size < 0 else "fed_hike"
                for e in [t for t in targets if get_dataset(t) and get_dataset(t).frequency == "daily"][:3]:  # type: ignore[union-attr]
                    add("run_event_study", dataset_id=e, event_type=etype)
    if re.search(r"event stud|react(ion|s)? (to|around|after)|after (the )?(fomc|fed)|around (fomc|fed)", q):
        etype = "fed_cut" if re.search(r"\bcuts?\b", q) else "fed_hike" if re.search(r"\bhikes?\b", q) else "fed_change"
        for e in [t for t in sourced if get_dataset(t).frequency == "daily"][:3]:  # type: ignore[union-attr]
            add("run_event_study", dataset_id=e, event_type=etype)
    if re.search(r"\bportfolio|\bmy (holdings|positions|book)\b|value at risk|\bvar\b|stress test", q):
        add("analyze_portfolio")
    if "anomaly_screen" in intents:
        ac = next((c for c in ("commodity", "fx", "rates", "equity") if c in q or (c == "commodity" and "commodit" in q)), None)
        add("screen_anomalies", **({"asset_class": ac} if ac else {}))
    if "comparison" in intents and not sourced and not country_codes:
        ac = "commodity" if "commodit" in q else "fx" if "currenc" in q else None
        add("compare_performance", **({"asset_class": ac} if ac else {}))
    fundamentals_q = re.search(r"\b(supply|demand|stocks?|stockpiles?|inventor(y|ies)|production|harvest|crops?|balance sheet|fundamentals?|wasde|usda|shortage|surplus|deficit|exporters?|producers?|tight(ness)?|glut|oversupplied|undersupplied)\b", q)
    if any(get_dataset(e).meta.get("asset_class") == "commodity" for e in primary):  # type: ignore[union-attr]
        commodities = [e for e in primary if get_dataset(e).meta.get("asset_class") == "commodity"]  # type: ignore[union-attr]
        for e in commodities[:3] if fundamentals_q else commodities[:1]:
            add("get_fundamentals", commodity=get_dataset(e).name)  # type: ignore[union-attr]
    from atlas.instruments import fundamentals_for

    extra = fundamentals_for(q)
    if extra and fundamentals_q and not any(fundamentals_for(get_dataset(e).name) == extra for e in primary):  # type: ignore[union-attr]
        add("get_fundamentals", commodity=extra.removeprefix("FUND.").lower())  # e.g. cotton or rice: no price series
    if fundamentals_q and country_codes:
        from atlas.instruments import countries as _countries

        names = {c["iso3"]: c["name"] for c in _countries()}
        for st in [st for st in steps if st["tool"] == "get_fundamentals"][:2]:
            add("get_fundamentals", commodity=st["args"]["commodity"], geography=names.get(country_codes[0], country_codes[0]))
        if depth != "quick":
            add("get_futures_curve", underlying=primary[0])
    if re.search(r"\b(calendar|upcoming|next (fed|fomc)|fomc meeting|fed meeting|when (is|does|will)|release dates?|data releases?|scheduled|what to watch)", q) \
            or ("scenario" in intents and _shock(q) and _shock(q)[0].startswith("RATE.")):
        add("get_economic_calendar", days=90 if re.search(r"\b(quarter|months?)\b", q) else 45)
    if "market_update" in intents and depth != "quick":
        add("search_documents", query=query)

    first = get_dataset(primary[0]) if primary else None
    return {
        "research_id": uuid.uuid4().hex,
        "query": query,
        "intent": intents[0],
        "intents": intents,
        "asset": None if first is None else {
            "dataset_id": first.dataset_id, "name": first.name,
            "asset_class": first.meta.get("asset_class"), "price_basis": first.meta.get("price_basis"),
        },
        "entities": entities + positioning_entities,
        "countries": country_codes,
        "coverage_gaps": gaps,
        "timeframe": {"lookback_years": lookback, "horizon_months": horizon, "focus": "latest_available"},
        "depth": depth,
        "steps": steps,
        "output": {"format": "research_report", "depth": depth, "citations_required": True},
    }


def validate_plan(plan: dict[str, Any]) -> list[str]:
    from atlas.research.tools import TOOLS

    problems = []
    known = datasets()
    for step in plan["steps"]:
        if step["tool"] not in TOOLS:
            problems.append(f"unknown tool {step['tool']}")
        for k in ("dataset_id", "series_id", "dependent", "asset", "driver"):
            if k in step["args"] and step["args"][k] not in known:
                problems.append(f"{step['tool']}: unknown dataset {step['args'][k]}")
        for k in ("dataset_ids", "explanatory"):
            for d in step["args"].get(k, []):
                if d not in known:
                    problems.append(f"{step['tool']}: unknown dataset {d}")
    return problems
