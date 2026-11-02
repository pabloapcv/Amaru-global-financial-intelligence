"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, type ChartSpec } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Stock = {
  name: string; unit: string; as_of: string; level: number; weekly_change: number; year_ago: number | null;
  five_year_average: number; five_year_low: number; five_year_high: number; vs_five_year_average_pct: number; position: string;
};
type Flow = { name: string; unit: string; as_of: string; four_week_average: number; four_week_average_year_ago: number | null; change_vs_year_ago: number | null };
type Inventories = {
  available: boolean; reason?: string; stocks?: Stock[]; flows?: Flow[]; caveat?: string;
  spr?: { as_of: string; level: number; change_12m: number | null } | null;
  chart?: { name: string; points: { date: string; value: number }[] }[];
};

const TITLE = "U.S. energy inventories";
const num = (v: number | null, d = 1) => (v == null ? "n/a" : v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
const signed = (v: number | null, d = 1) => (v == null ? "n/a" : `${v > 0 ? "+" : ""}${num(v, d)}`);

export default function EnergyInventories() {
  const [data, setData] = useState<Inventories | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { api<Inventories>("/api/v1/macro/energy").then(setData).catch(setError); }, []);

  if (error) return <Card title={TITLE}><ErrorNote error={error} /></Card>;
  if (!data) return <Card title={TITLE}><span className="muted">Loading…</span></Card>;
  if (!data.available) return <Card title={TITLE}><p className="muted" style={{ margin: 0 }}>Not available yet: {data.reason}. Run <code>atlas ingest</code>.</p></Card>;
  const spec: ChartSpec = { type: "line", title: "Stocks against their five-year average for the same week", unit: "%", series: data.chart! };
  return (
    <Card title={TITLE}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>Stocks against their five-year average for the same week, %</div>
      <Chart spec={spec} height={200} />
      <div style={{ overflowX: "auto", marginTop: 12 }}>
        <table>
          <thead><tr><th>Stocks</th><th className="num">Latest</th><th className="num">Week change</th><th className="num">5-year average</th><th className="num">Versus average</th><th>Position</th></tr></thead>
          <tbody>
            {data.stocks!.map((s) => (
              <tr key={s.name}>
                <td>{s.name}<div className="muted" style={{ fontSize: 11.5 }}>{s.unit}, week to {s.as_of}</div></td>
                <td className="num">{num(s.level)}</td>
                <td className="num">{signed(s.weekly_change)}</td>
                <td className="num" title={`Five-year range ${num(s.five_year_low)} to ${num(s.five_year_high)}`}>{num(s.five_year_average)}</td>
                <td className="num">{signed(s.vs_five_year_average_pct)}%</td>
                <td>{s.position}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.flows!.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: 12 }}>
          <table>
            <thead><tr><th>Flow, 4-week average</th><th className="num">Latest</th><th className="num">A year earlier</th><th className="num">Change</th></tr></thead>
            <tbody>
              {data.flows!.map((f) => (
                <tr key={f.name}>
                  <td>{f.name}<div className="muted" style={{ fontSize: 11.5 }}>{f.unit}, week to {f.as_of}</div></td>
                  <td className="num">{num(f.four_week_average)}</td>
                  <td className="num">{num(f.four_week_average_year_ago)}</td>
                  <td className="num">{signed(f.change_vs_year_ago)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.spr && (
        <p style={{ marginBottom: 0 }}>Strategic Petroleum Reserve: <strong>{num(data.spr.level)} million barrels</strong> in the week to {data.spr.as_of}
          {data.spr.change_12m != null && <>, {signed(data.spr.change_12m)} million from a year earlier</>}.</p>
      )}
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{data.caveat} Source: U.S. Energy Information Administration.</p>
    </Card>
  );
}
