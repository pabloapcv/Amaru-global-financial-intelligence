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


def _usd(v: float | None) -> str:
    return "n/a" if v is None else f"{'-' if v < 0 else ''}${abs(v):,.0f}"


def _sig(p: float | None) -> bool:
    return p is not None and p < 0.05


def _strength(r: float) -> str:
    a = abs(r)
    return "negligible" if a < 0.1 else "weak" if a < 0.3 else "moderate" if a < 0.6 else "strong"


def compose(query: str, plan: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for e in evidence:
        by_tool.setdefault(e["tool"], []).append(e)

    from atlas.research.templates import TEMPLATES

    tmpl = TEMPLATES.get(plan.get("template") or "research_report", {})
    if plan.get("template") and plan["template"] != "research_report":
        subject = None
        if plan["template"] == "commodity_deep_dive" and plan.get("asset"):
            subject = plan["asset"]["name"]
        elif plan["template"] == "country_intelligence":
            from atlas.instruments import countries

            iso = (plan.get("countries") or ["BRA"])[0]
            subject = next((c["name"] for c in countries() if c["iso3"] == iso), iso)
        title = f"# {tmpl['name']}" + (f": {subject}" if subject else "")
    else:
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
            summary.append(f"- {e['result']['name']}: {', '.join(bits)}; its 5-year percentile rank is {_fmt(p['percentile_in_5y_range'], 1)} [{e['label']}].")
    for e in by_tool.get("detect_anomalies", []):
        lt = e["result"]["latest"]
        if lt["is_anomalous"]:
            summary.append(f"- The latest move in {_name(e['result']['dataset_id'])} is statistically unusual (robust z {_fmt(lt['robust_z'])}) [{e['label']}].")
    for e in by_tool.get("scan_scenarios", []):
        r = e["result"]
        top = [s for s in r["scenarios"] if s.get("relevance")]
        if top:
            summary.append(f"- {len(r['scenarios'])} scenarios assessed across {len(r['universe'])} markets; the data points most to: "
                           + "; ".join(f"**{s['name']}** ({s['relevance']} signal{'s' if s['relevance'] != 1 else ''})" for s in top[:3])
                           + f" [{e['label']}].")
        summary.append(f"- {r['method']['caveat']}")
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

    # Scenarios ------------------------------------------------------------------
    for e in by_tool.get("scan_scenarios", []):
        r = e["result"]
        lines += ["## Scenarios", "",
                  f"Data as of {r['as_of']}. {r['considered']} candidate scenarios were checked against the data; "
                  f"{len(r['scenarios'])} are shown, ranked by how many data signals point to them [{e['label']}].", ""]
        if r.get("missing"):
            lines += [f"Not assessed (no stored data): {', '.join(_name(d) for d in r['missing'])}.", ""]
        ch = r.get("changes")
        if ch:
            lines += ["### What changed since the last scan", "", f"Compared with the scan of {ch['previous_scan_at'][:10]} (data as of {ch['previous_as_of']}) [{e['label']}]:", ""]
            bits = []
            bits += [f"- New: **{x['name']}** ({x['relevance']} signal{'s' if x['relevance'] != 1 else ''})" for x in ch["new"]]
            bits += [f"- Stronger: {x['name']} ({x['from']} → {x['to']} signals)" for x in ch["stronger"]]
            bits += [f"- Weaker: {x['name']} ({x['from']} → {x['to']} signals)" for x in ch["weaker"]]
            bits += [f"- No longer signalled: {x['name']}" for x in ch["dropped"]]
            lines += (bits or ["- Nothing changed: the same scenarios, with the same signals."]) + [""]
        for i, s in enumerate(r["scenarios"], 1):
            unit = "bp" if s["shock_unit"] == "bp" else "%"
            why = "; ".join(sig["text"] for sig in s["signals"]) or "no current signal (standard stress test)"
            lines += [f"### {i}. {s['name']}", "",
                      f"*Driver:* {s['driver_name']} {_fmt(s['shock'], 0)}{' ' if unit == 'bp' else ''}{unit}. *Why it is here:* {why} [{e['label']}].", ""]
            rows = [im for im in s["impacts"] if not im["is_driver"]]
            if rows:
                lines += ["| Market | Historical effect | 95% range | Reliable? |", "|---|---|---|---|"]
                for im in sorted(rows, key=lambda im: (not im["significant"], -abs(im["implied_move"]))):
                    u = " bp" if im["unit"] == "bp" else "%"
                    rng = f"{_fmt(im['ci95'][0])} to {_fmt(im['ci95'][1])}{u}" if im["ci95"] else "n/a"
                    lines.append(f"| {im['name']} | {_fmt(im['implied_move'])}{u} | {rng} | {'yes' if im['significant'] else 'no (p > 0.05)'} |")
                lines.append("")
                big = [next(im for im in rows if im["dataset_id"] == d) for d in s["clearest_effects"]]
                if big:
                    lines += ["Clearest effects (largest relative to their uncertainty): " + ", ".join(f"{im['name']} {_fmt(im['implied_move'])}{' bp' if im['unit'] == 'bp' else '%'}" for im in big)
                              + f" [{e['label']}].", ""]
        lines += [f"*Method:* {r['method']['selection']}. Effects: {r['method']['effects']}. {r['method']['caveat']}", ""]
        if r.get("next_fomc"):
            lines += [f"The next FOMC meeting is on {r['next_fomc']} [{e['label']}].", ""]

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

    # Trade -------------------------------------------------------------------------
    for e in by_tool.get("get_trade_flows", []):
        r = e["result"]
        lines += [f"## {r['name']}", ""]
        last = r["by_year"][-1] if r["by_year"] else None
        if last:
            lines.append(
                f"- In {last['year']}, exports were {_usd(last['exports_usd'])} and imports {_usd(last['imports_usd'])}, "
                f"a balance of {_usd(last['balance_usd'])} [{e['label']}]."
            )
        if r.get("latest_export_growth_pct") is not None:
            lines.append(f"- Exports changed {_fmt(r['latest_export_growth_pct'])}% on the prior year; compound annual growth {_fmt(r['export_cagr_pct'])}% over {r['export_cagr_years']} [{e['label']}].")
        for flow in ("exports", "imports"):
            pr = r["products"].get(flow)
            if pr:
                top = "; ".join(f"{t['description'].split(';')[0]} {_fmt(t['share_pct'], 1)}%" for t in pr["top"][:4])
                lines.append(f"- Top {flow} in {pr['year']} (HS chapters): {top}. Top three = {_fmt(pr['concentration_top3_pct'], 1)}% of the total [{e['label']}].")
        lines += ["", "*Reporter-declared values; the latest year may be incomplete or revised.*", ""]

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

    # Fundamentals ------------------------------------------------------------------
    fund = by_tool.get("get_fundamentals", [])
    if fund:
        lines += ["## Supply and demand (USDA)", ""]
        for e in fund:
            r = e["result"]
            cur = r["balance"][-1]
            ch, stu = r["change_vs_prior_year"], r["stocks_to_use"]
            prior = r["balance"][-2]["market_year"] if len(r["balance"]) > 1 else "the prior year"

            def chg(key: str) -> str:
                return f" ({ch[key]:+.2f}% vs {prior})" if ch.get(key) is not None else ""

            hist = (f", versus a {stu['history_years']}-year average of {_fmt(stu['mean_pct'])}% (percentile {_fmt(stu['percentile'], 1)})"
                    if stu.get("mean_pct") is not None else "")
            lines.append(
                f"- **{r['commodity']}, {r['geography']}, {cur['market_year']} ({cur['status']}, USDA report of {r['usda_release']}):** "
                f"production {_fmt(cur['production'], 0)}{chg('production_pct')}, consumption {_fmt(cur['consumption'], 0)}{chg('consumption_pct')}, "
                f"ending stocks {_fmt(cur['ending_stocks'], 0)}{chg('ending_stocks_pct')} ({r['unit']}). "
                f"Stocks-to-use {_fmt(stu['latest_pct'])}%{hist} [{e['label']}]."
            )
            if r.get("top_producers"):
                lines.append(f"- {r['commodity']} production is concentrated in "
                             + ", ".join(f"{t['country']} ({_fmt(t['share_pct'], 1)}%)" for t in r["top_producers"][:4])
                             + (f"; top exporters: " + ", ".join(f"{t['country']} ({_fmt(t['share_pct'], 1)}%)" for t in r["top_exporters"][:3]) if r.get("top_exporters") else "")
                             + f" [{e['label']}].")
            pv = r.get("price_vs_stocks_to_use") or {}
            if pv.get("implied_price") is not None:
                fit = (f"across {pv['n_years']} market years ({pv['first_market_year']}–{pv['last_market_year']}), stocks-to-use "
                       f"({pv['stocks_basis']}) and the real price have R² {_fmt(pv['r_squared'], 3)} ({pv['significance']})")
                if pv["has_signal"]:
                    band = pv.get(f"implied_price_band_{pv['band_pct']}") or [None, None]
                    lines.append(
                        f"- *Price versus fundamentals:* {fit}; each 1-point rise in stocks-to-use went with a {_fmt(pv['slope_pct_per_pp'])}% change in price. "
                        f"At the projected {_fmt(pv['projected_stocks_to_use_pct'])}% for {pv['projection_market_year']}, the fit implies "
                        f"{_fmt(pv['implied_price'])} {pv['price_unit']} ({pv['band_pct']}% band {_fmt(band[0])}–{_fmt(band[1])}); the latest price, "
                        f"{_fmt(pv['latest_price'])} on {pv['latest_price_date']}, is {_fmt(pv['latest_vs_implied_pct'])}% from that, "
                        f"{pv['assessment']}. This is an association, not a forecast [{e['label']}]."
                    )
                else:
                    lines.append(f"- *Price versus fundamentals:* {fit}, too weak to imply a price; fundamentals alone do not explain "
                                 f"{r['commodity'].lower()} prices in this sample [{e['label']}].")
            for v in r.get("revisions_vs_previous_report", [])[:3]:
                lines.append(f"- Since the {v['previous_release']} report, USDA revised {v['market_year']} {r['geography'].lower() if r['geography'] == 'World' else r['geography']} "
                             f"{v['attribute'].replace('_', ' ')} from {_fmt(v['previous'], 0)} to {_fmt(v['current'], 0)} ({_fmt(v['change_pct'])}%) [{e['label']}].")
        lines += ["", "*Stocks-to-use is ending stocks divided by consumption; a lower ratio means a tighter market. The latest market year is a USDA projection and will be revised.*", ""]

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

    # Portfolio ---------------------------------------------------------------------
    for e in by_tool.get("analyze_portfolio", []):
        r = e["result"]
        v = r["var"]
        lines += [f"## Portfolio risk: {r.get('portfolio_name') or 'portfolio'}", ""]
        lines.append(
            f"- Annualized volatility {_fmt(r['annualized_vol_pct'])}% from {r['n_observations']} {r['frequency']} returns; "
            f"diversification ratio {_fmt(r['diversification_ratio'], 3)}; max drawdown {_fmt(r['max_drawdown_pct'])}% [{e['label']}]."
        )
        lines.append(
            f"- Value at risk over {v['horizon']}: {_fmt(v['historical_95_pct'])}% at 95% (expected shortfall {_fmt(v['cvar_95_pct'])}%), "
            f"{_fmt(v['historical_99_pct'])}% at 99% [{e['label']}]."
        )
        top = sorted(r["holdings"], key=lambda h: -(h["risk_contribution_pct"] or 0))[:3]
        lines.append("- Largest risk contributors: " + "; ".join(
            f"{h['name']} {_fmt(h['risk_contribution_pct'])}% of risk at weight {_fmt(h['weight'], 4)}" for h in top) + f" [{e['label']}].")
        fx = r.get("factor_exposures")
        if fx:
            sig = [b for b in fx["betas"] if b["p_value"] is not None and b["p_value"] < 0.05]
            lines.append(f"- Factor exposures (R² {_fmt(fx['r_squared'], 4)}): " + ("; ".join(
                f"{_name(b['factor'])} beta {_fmt(b['beta'], 4)}" for b in sig) or "none significant at 5%") + f" [{e['label']}].")
        for st in r["stress_tests"]:
            if st["portfolio_return_pct"] is not None:
                cov = "" if st["weight_coverage_pct"] == 100 else f" (covers {_fmt(st['weight_coverage_pct'], 1)}% of weight)"
                lines.append(f"- Stress, {st['name']}: {_fmt(st['portfolio_return_pct'])}%{cov} [{e['label']}].")
        lines.append("")

    # Regimes -----------------------------------------------------------------------
    rg = by_tool.get("detect_regime", [])
    if rg:
        lines += ["## Volatility regime", ""]
        for e in rg:
            r = e["result"]
            lines.append(
                f"- {_name(r['dataset_id'])}: realized volatility {_fmt(r['realized_vol_annualized'])} {r['realized_vol_unit']} annualized, "
                f"percentile rank {_fmt(r['realized_vol_percentile'], 1)} in its history ({r['realized_vol_regime']}). "
                f"The Markov-switching model puts the probability of the high-volatility regime at {_fmt(r['markov_high_vol_probability_now'], 3)} "
                f"(regime volatilities {_fmt(r['markov_regime_vol_annualized']['low'])} vs {_fmt(r['markov_regime_vol_annualized']['high'])}) [{e['label']}]."
            )
        lines.append("")

    # Outlook ---------------------------------------------------------------------
    fc, sc, se = by_tool.get("run_forecast", []), by_tool.get("run_scenario", []), by_tool.get("get_seasonality", [])
    if fc or sc or se or by_tool.get("run_event_study"):
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
        for e in by_tool.get("run_event_study", []):
            r = e["result"]
            rx = r["reaction"]
            sig = "statistically significant" if _sig(rx["p_value"]) else "not statistically significant"
            lines.append(
                f"- *Historical precedent:* across {r['n_events']} {r['event_type'].replace('_', ' ')}s, {_name(r['dataset_id'])} moved "
                f"{_fmt(rx['mean'], 3)} {r['unit']} on average from the day before to day +{r['window']['post']} "
                f"(median {_fmt(rx['median'], 3)}, positive in {_fmt(rx['hit_rate_positive'] * 100, 1)}% of cases; {sig}, p={_fmt(rx['p_value'], 4)}) [{e['label']}]."
            )
        for e in sc:
            r = e["result"]
            lines.append(
                f"- *Hypothetical:* a {_fmt(r['shock'], 1)} {r['shock_unit']} move in {_name(r['driver'])} maps to {_fmt(r['implied_asset_move'], 3)} {r['implied_unit']} "
                f"in {_name(r['asset'])} (95% CI {_fmt(r['implied_asset_move_ci95'][0], 3)} to {_fmt(r['implied_asset_move_ci95'][1], 3)}; R² {_fmt(r['r_squared'], 4)}) [{e['label']}]."
            )
        lines.append("")

    # Calendar --------------------------------------------------------------------
    for e in by_tool.get("get_economic_calendar", []):
        r = e["result"]
        lines += [f"## What to watch ({r['from']} to {r['to']})", ""]
        for ev in r["events"]:
            when = ev["date"] + (f" to {ev['end_date']}" if ev.get("end_date") and ev["end_date"] != ev["date"] else "")
            extra = ", with the Summary of Economic Projections" if ev.get("with_projections") else ""
            covers = f" (covers {', '.join(_name(d) for d in ev['datasets'])})" if ev.get("datasets") else ""
            lines.append(f"- {when}: {ev['title'].replace(' (with projections)', '')}{extra}{covers} [{e['label']}].")
        if not r["events"]:
            lines.append(f"- No scheduled events are listed in this window [{e['label']}].")
        pending = [d for d in r["series_due"] if not d.get("official_release_date")]
        if pending:
            lines.append("- Next data due (reference period; no official release date stored): "
                         + "; ".join(f"{d['name']} for {d['next_period_due']}" for d in pending) + f" [{e['label']}].")
        lines.append("")

    # Official releases ------------------------------------------------------------
    docs = [(e, d) for e in by_tool.get("search_documents", []) for d in e["result"]["results"]]
    if docs:
        lines += ["## Recent official releases", ""]
        for e, d in docs[:8]:
            lines.append(f"- {d['published'] or 'undated'} · {d['publisher']}: [{d['title']}]({d['url']}) [{e['label']}].")
        lines.append("")

    # Limitations -----------------------------------------------------------------
    lim = []
    for e in evidence:
        if e["tool"].endswith("_unavailable"):
            lim.append(f"- {e['result']['reason']} [{e['label']}]")
    for e in by_tool.get("get_economic_calendar", []):
        lim += [f"- {n} [{e['label']}]" for n in e["result"]["notes"]]
    for e in by_tool.get("search_documents", []):
        found = "" if e["result"]["results"] else f" No release matched \"{e['result']['query']}\" in the last {e['result']['days']} days."
        lim.append(f"- Documents: {e['result']['coverage']}{found} [{e['label']}]")
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
