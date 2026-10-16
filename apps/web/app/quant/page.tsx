"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, type ChartSpec, type Instrument } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type Analysis = "correlation" | "regression" | "forecast" | "scenario" | "anomalies" | "seasonality" | "regime";

const HELP: Record<Analysis, string> = {
  correlation: "Correlation of period changes, aligned to the coarsest frequency, with confidence intervals and rolling stability.",
  regression: "OLS of the first series' changes on the others, Newey-West standard errors.",
  forecast: "Walk-forward-evaluated statistical forecast, compared with no-change and seasonal-naive baselines.",
  scenario: "Hypothetical: driver shock × historical beta. Shock in % for prices/FX, bp for yields.",
  anomalies: "Robust z-score of the latest change against a trailing median/MAD.",
  seasonality: "Average change by calendar month with hit rates.",
  regime: "Volatility regime: realized-vol percentile plus a 2-state Markov-switching model's probability of the high-volatility state.",
};

export default function QuantLab() {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [analysis, setAnalysis] = useState<Analysis>("correlation");
  const [ids, setIds] = useState<string[]>(["CMD.SUGAR.ISA", "FX.USDBRL", "CMD.CRUDE.BRENT"]);
  const [lookback, setLookback] = useState(5);
  const [horizon, setHorizon] = useState(3);
  const [shock, setShock] = useState(-50);
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
      const body: Record<string, unknown> = { analysis, lookback_years: lookback, horizon_months: horizon };
      if (analysis === "regression") Object.assign(body, { dependent: ids[0], explanatory: ids.slice(1) });
      else if (analysis === "scenario") Object.assign(body, { asset: ids[0], driver: ids[1], shock });
      else body.dataset_ids = multi ? ids : [ids[0]];
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
              {(Object.keys(HELP) as Analysis[]).map((a) => <option key={a} value={a}>{a}</option>)}
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
            <div><label>Lookback (years)</label><input type="number" min={1} max={40} value={lookback} onChange={(e) => setLookback(+e.target.value)} /></div>
            {analysis === "forecast" && <div><label>Horizon (months)</label><input type="number" min={1} max={24} value={horizon} onChange={(e) => setHorizon(+e.target.value)} /></div>}
            {analysis === "scenario" && <div><label>Shock (% or bp)</label><input type="number" value={shock} onChange={(e) => setShock(+e.target.value)} /></div>}
          </div>
          <button style={{ marginTop: 14 }} onClick={run} disabled={busy}>{busy ? "Running…" : "Run"}</button>
        </Card>
        <div className="stack">
          <ErrorNote error={error} />
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
