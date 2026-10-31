"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, type ChartSpec } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Pair = {
  crop: string; fertilizer: string; as_of: string; tons_of_crop: number; tons_12m_ago: number | null; change_12m_pct: number | null;
  median: number; percentile: number; reading: string;
};
type Affordability = {
  available: boolean; reason?: string; pairs?: Pair[]; expensive?: number; typical?: number; cheap?: number; caveat?: string;
  chart?: { name: string; points: { date: string; value: number }[] }[];
};

const TITLE = "Fertilizer affordability";
const tons = (v: number | null) => (v == null ? "n/a" : v.toFixed(v < 0.1 ? 3 : 2));
// Fertilizer costing more of the crop is bad news for growers.
const tone = (reading: string) => (reading === "expensive" ? "down" : reading === "cheap" ? "up" : "");

export default function FertilizerAffordability() {
  const [data, setData] = useState<Affordability | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { api<Affordability>("/api/v1/macro/fertilizer").then(setData).catch(setError); }, []);

  if (error) return <Card title={TITLE}><ErrorNote error={error} /></Card>;
  if (!data) return <Card title={TITLE}><span className="muted">Loading…</span></Card>;
  if (!data.available) return <Card title={TITLE}><p className="muted" style={{ margin: 0 }}>Not available yet: {data.reason}. Run <code>atlas ingest</code>.</p></Card>;
  const spec: ChartSpec = { type: "line", title: "Tons of crop to buy a ton of fertilizer", unit: "tons", series: data.chart! };
  return (
    <Card title={TITLE}>
      <p style={{ marginTop: 0 }}>Against its own history, fertilizer is <strong>expensive</strong> for {data.expensive} of {data.pairs!.length} crop pairs, typical for {data.typical} and cheap for {data.cheap}.</p>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>Tons of crop to buy a ton of fertilizer</div>
      <Chart spec={spec} height={200} />
      <div style={{ overflowX: "auto", marginTop: 12 }}>
        <table>
          <thead><tr><th>Crop</th><th>Fertilizer</th><th className="num">Tons of crop</th><th className="num">12 months earlier</th><th className="num">Usual</th><th className="num">Percentile</th><th>Reading</th></tr></thead>
          <tbody>
            {data.pairs!.map((p) => (
              <tr key={`${p.crop}-${p.fertilizer}`}>
                <td>{p.crop}<div className="muted" style={{ fontSize: 11.5 }}>{p.as_of.slice(0, 7)}</div></td>
                <td>{p.fertilizer}</td>
                <td className="num">{tons(p.tons_of_crop)}</td>
                <td className="num">{tons(p.tons_12m_ago)}</td>
                <td className="num">{tons(p.median)}</td>
                <td className="num">{p.percentile}</td>
                <td className={tone(p.reading)}>{p.reading}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{data.caveat} &quot;Usual&quot; is the median since each pair&apos;s data starts. Source: World Bank, IMF.</p>
    </Card>
  );
}
