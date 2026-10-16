"""Deterministic narrative writer.

Used when no LLM is configured (and as a fallback if the LLM call fails). It only restates
evidence, so every number it prints is traceable by construction. It does not attempt the
causal interpretation an analyst (or the LLM) would add, and says so.
"""

from __future__ import annotations

from typing import Any

from atlas import analytics
from atlas.instruments import get_dataset


def _name(dsid: str) -> str:
    ds = get_dataset(dsid)
    return ds.name if ds else dsid


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, (int, float)):
        if abs(v) >= 1000:
            return f"{v:,.{nd}f}"
        return f"{v:.{nd}f}" if nd else f"{v:.0f}"
    return str(v)


def _sig(p: float | None) -> bool:
    return p is not None and p < 0.05


def _strength(r: float) -> str:
    a = abs(r)
    return "negligible" if a < 0.1 else "weak" if a < 0.3 else "moderate" if a < 0.6 else "strong"


def compose(query: str, plan: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for e in evidence:
        by_tool.setdefault(e["tool"], []).append(e)

    title = f"# {plan['asset']['name']}: research brief" if plan.get("asset") else "# Market and macro research brief"
    lines = [title, ""]

    # Executive summary ---------------------------------------------------------
    summary: list[str] = []
    for e in by_tool.get("get_latest_quote", []):
        r = e["result"]
        chg = f", {_fmt(r.get('change_pct'))}% versus the prior observation ({r.get('previous_date')})" if r.get("change_pct") is not None else ""
        summary.append(
            f"- **{r['name']}** last printed **{_fmt(r['value'], 4)} {r['unit']}** on {r['observation_date']}{chg}. "
            f"Freshness: *{r['freshness']}*; basis: {r['price_basis'].replace('_', ' ')} [{e['label']}]."
        )
    for e in by_tool.get("get_price_history", []):
        p = e["result"]["performance"]
        t = p["trailing"]
        bits = []
        for k in ("3M", "1Y", "5Y"):
            if t.get(k) and "return_pct" in t[k]:
                bits.append(f"{_fmt(t[k]['return_pct'])}% over {k}")
            elif t.get(k) and "change_bp" in t[k]:
                bits.append(f"{_fmt(t[k]['change_bp'], 1)} bp over {k}")
        if bits:
            summary.append(f"- {e['result']['name']}: {', '.join(bits)}; it sits at the {_fmt(p['percentile_in_5y_range'], 1)}th percentile of its 5-year range [{e['label']}].")
    for e in by_tool.get("detect_anomalies", []):
        lt = e["result"]["latest"]
        if lt["is_anomalous"]:
            summary.append(f"- The latest move in {_name(e['result']['dataset_id'])} is statistically unusual (robust z {_fmt(lt['robust_z'])}) [{e['label']}].")
    if not summary:
        summary.append("- See the sections below; no single-instrument snapshot was requested.")
    lines += ["## Executive summary", "", *summary, ""]

    lines += [
        "## Scope and methodology", "",
        f"Question: *{query.rstrip('.')}*. Lookback {plan['timeframe']['lookback_years']:g} years; outlook horizon "
        f"{plan['timeframe']['horizon_months']} months. All figures come from stored observations and deterministic "
        "calculations in the ATLAS quant engine; each claim cites its evidence item. This brief was composed by the "
        "deterministic writer, which restates evidence without causal interpretation.", "",
    ]

    # Historical analysis -------------------------------------------------------
    hist = by_tool.get("get_price_history", []) + by_tool.get("get_macro_series", [])
    if hist:
        lines += ["## Historical analysis", ""]
        for e in hist:
            r = e["result"]
            p = r["performance"]
            lines.append(
                f"- **{r['name']}** ({r['frequency']}, {r['unit']}): between {r['window_start']} and {r['window_end']} it ranged "
                f"{_fmt(r['window_min'], 4)}–{_fmt(r['window_max'], 4)}."
                + (f" One-year annualised volatility {_fmt(p['annualized_vol_1y_pct'])}%." if p.get("annualized_vol_1y_pct") else "")
                + (f" Max drawdown over 5 years {_fmt(p['max_drawdown_5y_pct'])}%; current drawdown from peak {_fmt(p['current_drawdown_pct'])}%." if p.get("max_drawdown_5y_pct") is not None else "")
                + f" [{e['label']}]"
            )
            if r.get("yoy_pct_recent"):
                last = r["yoy_pct_recent"][-1]
                lines.append(f"  - Year-over-year change: {_fmt(last['yoy_pct'])}% for {last['period']} [{e['label']}].")
            if r.get("qoq_annualized_pct_recent"):
                last = r["qoq_annualized_pct_recent"][-1]
                lines.append(f"  - Quarter-over-quarter annualised growth: {_fmt(last['pct'])}% for {last['period']} [{e['label']}].")
            if r.get("monthly_change_recent"):
                last = r["monthly_change_recent"][-1]
                lines.append(f"  - Latest monthly change: {_fmt(last['change'], 1)} thousand for {last['period']} [{e['label']}].")
        lines.append("")

    comp = by_tool.get("compare_performance", [])
    if comp:
        lines += ["## Cross-asset snapshot", "", "| Dataset | Last | Date | 1M | 1Y | Unit |", "|---|---|---|---|---|---|"]
        for e in comp:
            for row in e["result"]["rows"]:
                t = row["trailing"]
                lines.append(f"| {row['name']} | {_fmt(row['last_value'], 4)} | {row['last_date']} | {_fmt(t.get('1M'))} | {_fmt(t.get('1Y'))} | {row['trailing_unit']} |")
        lines += ["", f"Source: [{', '.join(e['label'] for e in comp)}].", ""]

    # Countries ---------------------------------------------------------------------
    cc = by_tool.get("compare_countries", [])
    if cc:
        lines += ["## Country comparison", ""]
        for e in cc:
            r = e["result"]
            keys = list(r["indicators"])
            head = " | ".join(f"{r['indicators'][k]['name']} ({r['indicators'][k]['unit']})" for k in keys)
            lines += [f"| Country | {head} |", "|---|" + "---|" * len(keys)]
            for row in r["rows"]:
                cells = [(f"{_fmt(row[k]['latest'])} ({row[k]['year']}{', stale' if row[k].get('stale') else ''})") if row.get(k) else "n/a" for k in keys]
                lines.append(f"| {row['name']} | {' | '.join(cells)} |")
            lines.append("")
            for k in keys:
                order = r["rank_high_to_low"].get(k) or []
                if order:
                    names = {row["country"]: row["name"] for row in r["rows"]}
                    lines.append(f"- {r['indicators'][k]['name']}: highest {names[order[0]]}, lowest {names[order[-1]]} [{e['label']}].")
            if r.get("stale_values"):
                lines.append(f"- {len(r['stale_values'])} value(s) are much older than peer data and were excluded from rankings [{e['label']}].")
            lines += ["", f"*Latest available year per country; reference years {', '.join(str(y) for y in r['reference_years'])}. Source: World Bank WDI [{e['label']}].*", ""]
    for e in by_tool.get("get_country_profile", []):
        r = e["result"]
        lines += [f"## {r['name']}: economic profile", ""]
        for k, ind in r["indicators"].items():
            last = ind["recent"][-1]
            prev = ind["recent"][-2] if len(ind["recent"]) > 1 else None
            trend = f", from {_fmt(prev['value'])} in {prev['year']}" if prev else ""
            lines.append(f"- {ind['name']}: {_fmt(last['value'])} {ind['unit']} in {last['year']}{trend}; 10-year average {_fmt(ind['avg_10y'])} [{e['label']}].")
        lines.append("")

    # Positioning -------------------------------------------------------------------
    pos = by_tool.get("get_positioning", [])
    if pos:
        lines += ["## Positioning (CFTC Commitments of Traders)", ""]
        for e in pos:
            r = e["result"]
            extreme = f" — **{r['positioning_extreme']}**" if r.get("positioning_extreme") else ""
            lines.append(
                f"- {r['name']}: speculators (non-commercials) were net {_fmt(r['noncomm_net'], 0)} contracts on {r['report_date']} "
                f"({_fmt(r['noncomm_net_pct_oi'])}% of open interest), {_fmt(r['noncomm_net_change_1w'], 0)} on the week. "
                f"COT index {_fmt(r['cot_index'], 1)} (0 = most short, 100 = most long over the window), z-score {_fmt(r['net_zscore'])}{extreme}. "
                f"Commercial hedgers net {_fmt(r['comm_net'], 0)} [{e['label']}]."
            )
        lines += ["", "*Positioning describes who holds risk; crowded positioning raises reversal risk but is not a timing signal.*", ""]

    # Relationships ---------------------------------------------------------------
    rel = by_tool.get("calculate_correlation", []) + by_tool.get("run_regression", [])
    if rel:
        lines += ["## Drivers and relationships", ""]
        for e in by_tool.get("calculate_correlation", []):
            r = e["result"]
            focus = e["args"]["dataset_ids"][0]
            for p in r["pairs"]:
                if focus not in (p["a"], p["b"]):
                    continue  # driver-vs-driver pairs stay in the evidence appendix
                sig = "statistically significant" if _sig(p["pearson_p_value"]) else "not statistically significant"
                lines.append(
                    f"- {_name(p['a'])} vs {_name(p['b'])}: {_strength(p['pearson'])} correlation of {r['frequency']} changes "
                    f"(Pearson {_fmt(p['pearson'], 3)}, 95% CI {_fmt(p['pearson_ci95'][0], 3)} to {_fmt(p['pearson_ci95'][1], 3)}, {sig}); "
                    f"the {p['rolling_window']}-period rolling correlation has ranged {_fmt(p['rolling_min'], 3)} to {_fmt(p['rolling_max'], 3)} [{e['label']}]."
                )
        for e in by_tool.get("run_regression", []):
            r = e["result"]
            sig = [c for c in r["coefficients"] if c["variable"] != "const" and _sig(c["p_value"])]
            desc = "; ".join(f"{_name(c['variable'])} β={_fmt(c['coefficient'], 3)} (p={_fmt(c['p_value'], 4)})" for c in sig) or "no explanatory variable is significant at 5%"
            lines.append(
                f"- Regression of {_name(r['dependent'])} changes ({r['n_observations']} {r['frequency']} observations, R² {_fmt(r['r_squared'], 4)}): {desc} [{e['label']}]."
            )
        lines += ["", "*These are associations between price changes; they do not establish causation.*", ""]

    # Risk --------------------------------------------------------------------------
    an = by_tool.get("detect_anomalies", []) + by_tool.get("screen_anomalies", [])
    if an:
        lines += ["## Unusual moves", ""]
        for e in by_tool.get("detect_anomalies", []):
            lt = e["result"]["latest"]
            lines.append(
                f"- {_name(e['result']['dataset_id'])}: latest change {_fmt(lt['change'], 3)} {lt['change_unit']} on {lt['date']} "
                f"(robust z {_fmt(lt['robust_z'])}; {'anomalous' if lt['is_anomalous'] else 'within normal range'}) [{e['label']}]."
            )
        for e in by_tool.get("screen_anomalies", []):
            flagged = e["result"]["flagged"]
            if flagged:
                for row in flagged[:10]:
                    lines.append(f"- Flagged: {row['name']} — {_fmt(row['change'], 3)} {row['change_unit']} on {row['date']} (robust z {_fmt(row['robust_z'])}) [{e['label']}].")
            else:
                lines.append(f"- No dataset's latest move exceeded |z| ≥ {_fmt(e['result']['threshold'], 1)} [{e['label']}].")
        lines.append("")

    # Outlook ---------------------------------------------------------------------
    fc, sc, se = by_tool.get("run_forecast", []), by_tool.get("run_scenario", []), by_tool.get("get_seasonality", [])
    if fc or sc or se:
        lines += ["## Outlook and scenarios", ""]
        for e in fc:
            r = e["result"]
            end = r["path"][-1]
            band = f" (p10–p90 {_fmt(end.get('p10'), 4)}–{_fmt(end.get('p90'), 4)})" if end.get("p10") is not None else ""
            verdict = "beats" if r["beats_naive_baseline"] else "does **not** beat"
            lines.append(
                f"- {_name(r['dataset_id'])}: the {analytics.METHOD_NAMES.get(r['selected_method'], r['selected_method'])} model points to {_fmt(end['point'], 4)} for {end['period']}{band}, "
                f"from {_fmt(r['base_value'], 4)} in {r['base_period']}. In walk-forward testing it {verdict} the naive baseline [{e['label']}]."
            )
        for e in se:
            months = sorted(e["result"]["by_month"], key=lambda m: m["mean_change"])
            if months:
                lo, hi = months[0], months[-1]
                lines.append(f"- Seasonality in {_name(e['result']['dataset_id'])}: weakest calendar month on average is month {lo['month']} ({_fmt(lo['mean_change'], 3)}), strongest is month {hi['month']} ({_fmt(hi['mean_change'], 3)}) [{e['label']}].")
        for e in sc:
            r = e["result"]
            lines.append(
                f"- *Hypothetical:* a {_fmt(r['shock'], 1)} {r['shock_unit']} move in {_name(r['driver'])} maps to {_fmt(r['implied_asset_move'], 3)} {r['implied_unit']} "
                f"in {_name(r['asset'])} (95% CI {_fmt(r['implied_asset_move_ci95'][0], 3)} to {_fmt(r['implied_asset_move_ci95'][1], 3)}; R² {_fmt(r['r_squared'], 4)}) [{e['label']}]."
            )
        lines.append("")

    # Limitations -----------------------------------------------------------------
    lim = []
    for e in evidence:
        if e["tool"].endswith("_unavailable"):
            lim.append(f"- {e['result']['reason']} [{e['label']}]")
    for e in by_tool.get("get_data_status", []):
        for d in e["result"]["datasets"]:
            if d["freshness"] not in ("current",):
                lim.append(f"- {d['name']}: {d['freshness']} — {d['freshness_note']} [{e['label']}]")
    for g in [] if by_tool.get("get_data_status") else plan.get("coverage_gaps", []):
        ds = get_dataset(g)
        lim.append(f"- {ds.name if ds else g}: {ds.meta.get('coverage_note') if ds else 'not covered'}")
    lim.append("- Prices are public reference/benchmark series published with a lag, not live tradable quotes.")
    lines += ["## Data limitations", "", *dict.fromkeys(lim), ""]
    return "\n".join(lines)
