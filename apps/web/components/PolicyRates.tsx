"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, type ChartSpec } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Bank = {
  area: string; name: string; rate: number; as_of: string; stale: boolean; last_move: { date: string; change_bp: number } | null;
  change_12m_bp: number | null; cuts_12m: number; hikes_12m: number; direction: string | null;
};
type Rates = {
  available: boolean; reason?: string; as_of?: string; banks?: Bank[]; caveat?: string;
  direction?: { months: number; banks: number; cutting: number; hiking: number; on_hold: number; path: { date: string; value: number }[] };
};

const bp = (v: number | null) => (v == null ? "n/a" : `${v > 0 ? "+" : ""}${v.toFixed(Number.isInteger(v) ? 0 : 1)} bp`);
const tone = (v: number | null | undefined) => (!v ? "" : v > 0 ? "up" : "down");

export default function PolicyRates() {
  const [data, setData] = useState<Rates | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { api<Rates>("/api/v1/macro/policy-rates").then(setData).catch(setError); }, []);

  if (error) return <Card title="Central bank policy rates"><ErrorNote error={error} /></Card>;
  if (!data) return <Card title="Central bank policy rates"><span className="muted">Loading…</span></Card>;
  if (!data.available) return <Card title="Central bank policy rates"><p className="muted" style={{ margin: 0 }}>Not available yet: {data.reason}. Run <code>atlas ingest</code>.</p></Card>;
  const d = data.direction!;
  const spec: ChartSpec = { type: "line", title: `Central banks raising minus cutting over ${d.months} months`, unit: "% of banks",
    series: [{ name: "Raising minus cutting", points: d.path }] };
  return (
    <Card title="Central bank policy rates">
      <p style={{ marginTop: 0 }}>Compared with {d.months} months earlier, <strong>{d.cutting} of {d.banks}</strong> central banks have lower rates, <strong>{d.hiking}</strong> higher and <strong>{d.on_hold}</strong> unchanged. Data through {data.as_of}.</p>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>Banks raising minus banks cutting, % of banks, over {d.months} months</div>
      <Chart spec={spec} height={200} />
      <div style={{ overflowX: "auto", marginTop: 12 }}>
        <table>
          <thead><tr><th>Central bank</th><th className="num">Rate</th><th>Latest move</th><th className="num">12 months</th><th>Direction</th></tr></thead>
          <tbody>
            {data.banks!.map((b) => (
              <tr key={b.area}>
                <td>{b.name}{b.stale && <div className="muted" style={{ fontSize: 11.5 }}>Data stops at {b.as_of}</div>}</td>
                <td className="num">{b.rate.toFixed(2)}%</td>
                <td style={{ whiteSpace: "nowrap" }}>{b.last_move ? <><span className={tone(b.last_move.change_bp)}>{bp(b.last_move.change_bp)}</span> <span className="muted" style={{ whiteSpace: "nowrap" }}>{b.last_move.date}</span></> : <span className="muted">none</span>}</td>
                <td className={`num ${tone(b.change_12m_bp)}`}>{bp(b.change_12m_bp)}</td>
                <td>{b.direction ?? "n/a"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{data.caveat} Germany, France and Italy use the euro area rate. Argentina, Bolivia, Ecuador, Uruguay and Paraguay are not covered. Source: BIS.</p>
    </Card>
  );
}
