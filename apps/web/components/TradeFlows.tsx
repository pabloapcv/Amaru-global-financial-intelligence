"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, fmt, type ChartSpec } from "@/lib/api";
import { Card, ErrorNote, FreshnessBadge } from "@/components/ui";

type Pair = { reporter: string; partner: string; dataset_id: string; name: string; observation_date: string | null; freshness: any };
type Product = { hs_chapter: string; description: string; value_usd: number; share_pct: number };
type Trade = {
  result: {
    name: string;
    by_year: { year: number; exports_usd: number | null; imports_usd: number | null; balance_usd: number | null }[];
    latest_export_growth_pct: number | null;
    export_cagr_pct: number | null;
    export_cagr_years: string | null;
    products: Record<string, { year: number; top: Product[]; concentration_top3_pct: number }>;
  };
  chart: ChartSpec;
  citation: string;
};

const usd = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v < 0 ? "-" : ""}$${(Math.abs(v) / 1e6).toLocaleString(undefined, { maximumFractionDigits: 1 })}M`;

function ProductTable({ title, data }: { title: string; data?: { year: number; top: Product[]; concentration_top3_pct: number } }) {
  if (!data) return null;
  return (
    <div>
      <h3 style={{ margin: "6px 0" }}>{title} · {data.year} · top 3 = {fmt(data.concentration_top3_pct, 1)}%</h3>
      <table>
        <tbody>
          {data.top.map((p) => (
            <tr key={p.hs_chapter}>
              <td className="mono muted" style={{ width: 34 }}>{p.hs_chapter}</td>
              <td style={{ fontSize: 12.5 }}>{p.description.split(";")[0]}</td>
              <td className="num">{usd(p.value_usd)}</td>
              <td className="num muted">{fmt(p.share_pct, 1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function TradeFlows() {
  const [pairs, setPairs] = useState<Pair[]>([]);
  const [selected, setSelected] = useState("USA:WLD");
  const [data, setData] = useState<Trade | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api<Pair[]>("/api/v1/trade/pairs").then(setPairs).catch(setError);
  }, []);
  useEffect(() => {
    const [reporter, partner] = selected.split(":");
    setData(null);
    setError(null);
    api<Trade>(`/api/v1/trade?reporter=${reporter}&partner=${partner}`).then(setData).catch(setError);
  }, [selected]);

  const current = pairs.find((p) => `${p.reporter}:${p.partner}` === selected);
  const last = data?.result.by_year.at(-1);

  return (
    <Card
      title="Trade flows · UN Comtrade"
      actions={
        <div className="row">
          {current?.freshness && <FreshnessBadge value={current.freshness} />}
          <select value={selected} onChange={(e) => setSelected(e.target.value)} style={{ width: "auto" }}>
            {pairs.map((p) => <option key={p.dataset_id} value={`${p.reporter}:${p.partner}`}>{p.name.replace("Trade: ", "")}</option>)}
          </select>
        </div>
      }
    >
      <ErrorNote error={error} />
      {!data ? (!error && <span className="muted">Loading…</span>) : (
        <>
          <div className="grid grid-4" style={{ marginBottom: 10 }}>
            <div className="quote"><div className="sub">Exports {last?.year}</div><div className="value" style={{ fontSize: 18 }}>{usd(last?.exports_usd)}</div></div>
            <div className="quote"><div className="sub">Imports {last?.year}</div><div className="value" style={{ fontSize: 18 }}>{usd(last?.imports_usd)}</div></div>
            <div className="quote"><div className="sub">Balance</div><div className={`value ${(last?.balance_usd ?? 0) < 0 ? "down" : "up"}`} style={{ fontSize: 18 }}>{usd(last?.balance_usd)}</div></div>
            <div className="quote"><div className="sub">Export growth y/y · CAGR {data.result.export_cagr_years}</div><div className="value" style={{ fontSize: 18 }}>{fmt(data.result.latest_export_growth_pct, 1)}% · {fmt(data.result.export_cagr_pct, 1)}%</div></div>
          </div>
          <Chart spec={data.chart} height={220} />
          <div className="grid grid-2" style={{ marginTop: 10 }}>
            <ProductTable title="Top exports" data={data.result.products.exports} />
            <ProductTable title="Top imports" data={data.result.products.imports} />
          </div>
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>Reporter-declared values (exports FOB, imports usually CIF). The latest year may be incomplete or revised. HS chapters are broad product groups.</p>
        </>
      )}
    </Card>
  );
}
