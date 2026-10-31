"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, type ChartSpec } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Input = { input: string; name: string; reading: number; weight: number };
type Record_ = { auc: number; brier: number; brier_average_only: number; first_month: string; last_month: string; months_scored: number; months_high: number; months_high_no_recession: number; recessions_covered?: number; beats_average?: boolean };
type Market = { dataset_id: string; name: string; unit: string; n: number; too_few: boolean; median?: number; low?: number; high?: number };
type Cycle = {
  available: boolean; reason?: string; caveat?: string;
  recession?: { as_of: string; probability_pct: number; inputs: Input[]; record: Record_; simple_model: Record_; path: { date: string; value: number }[]; recession_months: string[] };
  similar?: { as_of: string; now: Record<string, number>; reading_names: Record<string, string>; markets: Market[];
    periods: { month: string; distance: number; recession_within_12m: boolean | null; readings: Record<string, number> }[] };
};

const signed = (v: number, unit: string) => `${v > 0 ? "+" : ""}${v.toFixed(unit === "bp" ? 0 : 1)}${unit === "bp" ? " bp" : "%"}`;

export default function BusinessCycle() {
  const [data, setData] = useState<Cycle | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { api<Cycle>("/api/v1/macro/cycle").then(setData).catch(setError); }, []);

  if (error) return <Card title="Business cycle"><ErrorNote error={error} /></Card>;
  if (!data) return <Card title="Business cycle"><span className="muted">Estimating…</span></Card>;
  if (!data.available) return <Card title="Business cycle"><p className="muted" style={{ margin: 0 }}>Not available yet: {data.reason}. Run <code>atlas ingest</code>.</p></Card>;
  const r = data.recession!, s = data.similar!;
  const inRecession = new Set(r.recession_months);
  const spec: ChartSpec = { type: "line", title: "Estimate made at the time, and NBER recessions", unit: "%",
    series: [{ name: "12-month recession probability", points: r.path },
      { name: "NBER recession (100 = in recession)", points: r.path.map((p) => ({ date: p.date, value: inRecession.has(p.date) ? 100 : 0 })) }] };
  const rec = r.record, simple = r.simple_model;
  return (
    <Card title="Business cycle">
      <div className="grid grid-2">
        <div>
          <div className="muted" style={{ fontSize: 12 }}>U.S. recession within 12 months · data through {r.as_of.slice(0, 7)}</div>
          <div style={{ fontSize: 30, fontWeight: 600, margin: "2px 0 8px" }}>{r.probability_pct.toFixed(1)}%</div>
          <table style={{ fontSize: 12.5 }}>
            <tbody>{r.inputs.map((i) => <tr key={i.input}><td>{i.name}</td><td className="num">{i.reading.toFixed(2)}</td></tr>)}</tbody>
          </table>
          <p className="muted" style={{ fontSize: 12.5 }}>
            <strong>Track record:</strong> re-estimated monthly from {rec.first_month.slice(0, 4)} using only what was knowable then: AUC {rec.auc.toFixed(2)} (0.5 is a coin flip),
            Brier score {rec.brier.toFixed(3)} against {rec.brier_average_only.toFixed(3)} for always guessing the average. That covers only {rec.recessions_covered} recessions.
            {rec.months_high > 0 && ` It said 50% or more in ${rec.months_high} months, and in ${rec.months_high_no_recession} of them no recession followed within 12 months.`}
            Without the labour inputs: AUC {simple.auc.toFixed(2)}, Brier {simple.brier.toFixed(3)}.
          </p>
        </div>
        <Chart spec={spec} height={240} />
      </div>
      <div style={{ fontWeight: 600, margin: "14px 0 6px" }}>Similar periods</div>
      <div className="grid grid-2">
        <div style={{ overflowX: "auto" }}>
          <table style={{ fontSize: 12.5 }}>
            <thead><tr><th>Month</th><th className="num">Distance</th><th>Recession within 12 months?</th></tr></thead>
            <tbody>{s.periods.map((p) => (
              <tr key={p.month}><td>{p.month.slice(0, 7)}</td><td className="num">{p.distance.toFixed(2)}</td>
                <td>{p.recession_within_12m === null ? "Not yet known" : p.recession_within_12m ? "Yes" : "No"}</td></tr>
            ))}</tbody>
          </table>
        </div>
        <div style={{ overflowX: "auto" }}>
          <table style={{ fontSize: 12.5 }}>
            <thead><tr><th>Next 12 months</th><th className="num">Median</th><th className="num">Range</th></tr></thead>
            <tbody>{s.markets.map((m) => (
              <tr key={m.dataset_id}><td>{m.name}</td>
                {m.too_few ? <td className="num muted" colSpan={2}>too few periods</td> : <>
                  <td className="num">{signed(m.median!, m.unit)}</td>
                  <td className="num muted" style={{ whiteSpace: "nowrap" }}>{signed(m.low!, m.unit)} to {signed(m.high!, m.unit)}</td></>}
              </tr>
            ))}</tbody>
          </table>
        </div>
      </div>
      <p className="muted" style={{ fontSize: 11.5, marginBottom: 0 }}>{data.caveat}</p>
    </Card>
  );
}
