"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { PriceScatter, type PriceModel } from "@/components/Fundamentals";
import { api, fmt, type ChartSpec, type Instrument } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type Analysis = "correlation" | "regression" | "forecast" | "scenario" | "anomalies" | "seasonality" | "regime" | "event_study" | "fundamentals";

const HELP: Record<Analysis, string> = {
  correlation: "Correlation of period changes, aligned to the coarsest frequency, with confidence intervals and rolling stability.",
  regression: "OLS of the first series' changes on the others, Newey-West standard errors.",
  forecast: "Walk-forward-evaluated statistical forecast, compared with no-change and seasonal-naive baselines.",
  scenario: "Hypothetical: driver shock × historical beta. Shock in % for prices/FX, bp for yields.",
  anomalies: "Robust z-score of the latest change against a trailing median/MAD.",
  seasonality: "Average change by calendar month with hit rates.",
  regime: "Volatility regime: realized-vol percentile plus a 2-state Markov-switching model's probability of the high-volatility state.",
  event_study: "Average cumulative abnormal move around Fed decisions (inferred from the effective fed funds rate since 2009). Daily series only.",
  fundamentals: "Real price against USDA stocks-to-use across market years: the price the current projection implies, an 80% band, and where today's price sits. Sugar, coffee, corn, wheat and soybeans.",
};
const LABELS: Partial<Record<Analysis, string>> = { event_study: "event study", fundamentals: "price vs stocks-to-use (USDA)" };

export default function QuantLab() {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [analysis, setAnalysis] = useState<Analysis>("correlation");
  const [ids, setIds] = useState<string[]>(["CMD.SUGAR.ISA", "FX.USDBRL", "CMD.CRUDE.BRENT"]);
  const [lookback, setLookback] = useState(5);
  const [horizon, setHorizon] = useState(3);
  const [shock, setShock] = useState(-50);
  const [eventType, setEventType] = useState("fed_cut");
  const [basis, setBasis] = useState("");
  const [result, setResult] = useState<{ result: any; citation: string; chart: ChartSpec | null } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Instrument[]>("/api/v1/instruments").then((all) => setInstruments(all.filter((i) => i.has_source && (i.kind === "market" || i.dataset_id.startsWith("US."))))).catch(setError);
  }, []);

  const multi = analysis === "correlation" || analysis === "regression";
  const two = analysis === "scenario";

  const run = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const body: Record<string, unknown> = { analysis, lookback_years: lookback, horizon_months: horizon, event_type: eventType };
      if (analysis === "regression") Object.assign(body, { dependent: ids[0], explanatory: ids.slice(1) });
      else if (analysis === "scenario") Object.assign(body, { asset: ids[0], driver: ids[1], shock });
      else body.dataset_ids = multi ? ids : [ids[0]];
      if (analysis === "fundamentals" && basis) body.stocks_basis = basis;
      setResult(await api("/api/v1/quant/analyze", { method: "POST", body: JSON.stringify(body) }));
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const slots = multi ? Math.max(ids.length, 2) : two ? 2 : 1;
  const setId = (i: number, v: string) => setIds((cur) => { const n = [...cur]; n[i] = v; return n.filter(Boolean); });

  return (
    <>
      <PageHeader title="Quant Lab" subtitle="Run the same deterministic calculations the research engine uses, directly." />
      <div className="split">
        <Card title="Analysis">
          <div className="field">
            <label>Method</label>
            <select value={analysis} onChange={(e) => setAnalysis(e.target.value as Analysis)}>
              {(Object.keys(HELP) as Analysis[]).map((a) => <option key={a} value={a}>{LABELS[a] ?? a}</option>)}
            </select>
            <p className="muted" style={{ fontSize: 12 }}>{HELP[analysis]}</p>
          </div>
          {Array.from({ length: slots }).map((_, i) => (
            <div className="field" key={i}>
              <label>{analysis === "regression" ? (i === 0 ? "Dependent" : `Explanatory ${i}`) : two ? (i === 0 ? "Asset" : "Driver") : `Series ${i + 1}`}</label>
              <select value={ids[i] ?? ""} onChange={(e) => setId(i, e.target.value)}>
                <option value="">—</option>
                {instruments.map((x) => <option key={x.dataset_id} value={x.dataset_id}>{x.name}</option>)}
              </select>
            </div>
          ))}
          {multi && ids.length < 5 && <button className="secondary" style={{ marginTop: 8 }} onClick={() => setIds([...ids, instruments[0]?.dataset_id])}>Add series</button>}
          <div className="grid grid-2" style={{ marginTop: 12 }}>
            {analysis !== "fundamentals" && <div><label>Lookback (years)</label><input type="number" min={1} max={40} value={lookback} onChange={(e) => setLookback(+e.target.value)} /></div>}
            {analysis === "forecast" && <div><label>Horizon (months)</label><input type="number" min={1} max={24} value={horizon} onChange={(e) => setHorizon(+e.target.value)} /></div>}
            {analysis === "scenario" && <div><label>Shock (% or bp)</label><input type="number" value={shock} onChange={(e) => setShock(+e.target.value)} /></div>}
            {analysis === "fundamentals" && (
              <div><label>Stocks basis</label>
                <select value={basis} onChange={(e) => setBasis(e.target.value)}>
                  <option value="">Convention (ex-China for grains and oilseeds)</option><option value="world">World</option><option value="world_ex_china">World excluding China</option>
                </select>
              </div>
            )}
            {analysis === "event_study" && (
              <div><label>Events</label>
                <select value={eventType} onChange={(e) => setEventType(e.target.value)}>
                  <option value="fed_cut">Fed cuts</option><option value="fed_hike">Fed hikes</option><option value="fed_change">All Fed changes</option>
                </select>
              </div>
            )}
          </div>
          <button style={{ marginTop: 14 }} onClick={run} disabled={busy}>{busy ? "Running…" : "Run"}</button>
        </Card>
        <div className="stack">
          <ErrorNote error={error} />
          {analysis === "fundamentals" && result?.result?.price_vs_stocks_to_use && (() => {
            const m = result.result.price_vs_stocks_to_use as PriceModel;
            if (!m.points) return <Card title="Price vs stocks-to-use"><div className="note note-warn">{m.reason ?? result.result.reason}</div></Card>;
            const band = m[`implied_price_band_${m.band_pct}`] as number[];
            return (
              <Card title={`${result.result.commodity}: real price vs ${m.stocks_basis} stocks-to-use`}>
                <PriceScatter m={m} />
                <dl className="kv" style={{ marginTop: 10 }}>
                  <dt>Fit</dt><dd>{m.n_years} market years ({m.first_market_year}–{m.last_market_year}), R² {fmt(m.r_squared, 3)}, {m.significance}; {fmt(m.slope_pct_per_pp, 2)}% per point of stocks-to-use</dd>
                  <dt>Basis</dt><dd>{m.stocks_basis}{m.stocks_basis_is_convention ? " (convention)" : " (override)"}</dd>
                  <dt>{m.projection_market_year}</dt><dd>projected stocks-to-use {fmt(m.projected_stocks_to_use_pct, 2)}%{m.has_signal ? <>; implied {fmt(m.implied_price, 2)} {m.price_unit} (80% band {fmt(band[0], 2)}–{fmt(band[1], 2)})</> : "; too weak to imply a price"}</dd>
                  <dt>Latest price</dt><dd>{fmt(m.latest_price, 2)} on {m.latest_price_date}{m.has_signal ? ` (${m.latest_vs_implied_pct > 0 ? "+" : ""}${fmt(m.latest_vs_implied_pct, 2)}%)` : ""}: {m.assessment}</dd>
                </dl>
                <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{(m.caveats as string[] | undefined)?.join(" ")}</p>
              </Card>
            );
          })()}
          {result?.chart && <Card title={result.chart.title}><Chart spec={result.chart} /></Card>}
          {result && (
            <Card title="Result">
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>{result.citation}</p>
              <pre>{JSON.stringify(result.result, (k, v) => (k === "rolling_series" ? undefined : v), 2)}</pre>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
