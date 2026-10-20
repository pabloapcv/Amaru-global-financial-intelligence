"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, fmt, type ChartSpec } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Row = { market_year: string; status: string; production: number | null; consumption: number | null; exports: number | null; ending_stocks: number | null; stocks_to_use_pct: number | null };
type Share = { country: string; value: number; share_pct: number };
type Result = {
  commodity: string; geography: string; unit: string; usda_release: string; balance: Row[];
  change_vs_prior_year: Record<string, number | null>;
  stocks_to_use: { latest_pct: number | null; mean_pct: number | null; percentile: number | null; history_years: number };
  top_producers?: Share[]; top_exporters?: Share[];
  revisions_vs_previous_report: { market_year: string; attribute: string; previous: number; current: number; change_pct: number | null; previous_release: string }[];
};

function signed(v: number | null | undefined, unit = "%") {
  if (v == null) return "—";
  return `${v > 0 ? "+" : ""}${fmt(v, 2)}${unit}`;
}

export default function Fundamentals({ id }: { id: string }) {
  const [data, setData] = useState<{ result: Result; chart: ChartSpec; citation: string } | null>(null);
  const [missing, setMissing] = useState(false);
  const [country, setCountry] = useState("");
  const [applied, setApplied] = useState("");
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { setCountry(""); setApplied(""); }, [id]);
  useEffect(() => {
    setData(null);
    setMissing(false);
    setError(null);
    api<{ result: Result; chart: ChartSpec; citation: string }>(`/api/v1/fundamentals?id=${encodeURIComponent(id)}${applied ? `&country=${encodeURIComponent(applied)}` : ""}`)
      .then(setData)
      .catch((e) => (e?.status === 404 ? setMissing(true) : setError(e)));
  }, [id, applied]);

  if (missing) return null;
  const r = data?.result;
  return (
    <Card
      title={r ? `Supply and demand · USDA · report of ${r.usda_release}` : "Supply and demand · USDA"}
      actions={
        <form className="row" onSubmit={(e) => { e.preventDefault(); setApplied(country.trim()); }}>
          <input value={country} onChange={(e) => setCountry(e.target.value)} placeholder="World, or a country" style={{ width: 160 }} aria-label="Country" />
          <button type="submit" className="secondary">Show</button>
        </form>
      }
    >
      <ErrorNote error={error} />
      {!r && !error && <span className="muted">Loading…</span>}
      {r && data && (
        <>
          <div className="grid grid-2" style={{ marginBottom: 10 }}>
            <dl className="kv">
              <dt>Geography</dt><dd>{r.geography}</dd>
              <dt>Stocks-to-use</dt><dd>{fmt(r.stocks_to_use.latest_pct, 2)}% <span className="muted">({signed(r.change_vs_prior_year.stocks_to_use_change_pp, " pp")} y/y)</span></dd>
              <dt>{r.stocks_to_use.history_years}-year average</dt><dd>{fmt(r.stocks_to_use.mean_pct, 2)}% · percentile {fmt(r.stocks_to_use.percentile, 0)}</dd>
            </dl>
            <dl className="kv">
              <dt>Production</dt><dd>{signed(r.change_vs_prior_year.production_pct)} y/y</dd>
              <dt>Consumption</dt><dd>{signed(r.change_vs_prior_year.consumption_pct)} y/y</dd>
              <dt>Ending stocks</dt><dd>{signed(r.change_vs_prior_year.ending_stocks_pct)} y/y</dd>
            </dl>
          </div>
          <Chart spec={data.chart} height={220} />
          <div style={{ overflowX: "auto", marginTop: 10 }}>
          <table>
            <thead><tr><th>Market year</th><th className="num">Production</th><th className="num">Consumption</th><th className="num">Exports</th><th className="num">Ending stocks</th><th className="num">Stocks/use</th></tr></thead>
            <tbody>
              {r.balance.map((b) => (
                <tr key={b.market_year}>
                  <td>{b.market_year}{b.status !== "historical" && <div className="muted" style={{ fontSize: 11 }}>{b.status}</div>}</td>
                  <td className="num">{fmt(b.production, 0)}</td><td className="num">{fmt(b.consumption, 0)}</td>
                  <td className="num">{fmt(b.exports, 0)}</td><td className="num">{fmt(b.ending_stocks, 0)}</td>
                  <td className="num">{fmt(b.stocks_to_use_pct, 1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          {r.top_producers && r.top_producers.length > 0 && (
            <p className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>
              Top producers: {r.top_producers.map((t) => `${t.country} ${fmt(t.share_pct, 1)}%`).join(" · ")}
              {r.top_exporters?.length ? <><br />Top exporters: {r.top_exporters.map((t) => `${t.country} ${fmt(t.share_pct, 1)}%`).join(" · ")}</> : null}
            </p>
          )}
          {r.revisions_vs_previous_report.length > 0 && (
            <p className="muted" style={{ fontSize: 12.5 }}>
              Revisions since the previous USDA report: {r.revisions_vs_previous_report.map((v) => `${v.market_year} ${v.attribute.replace("_", " ")} ${signed(v.change_pct)}`).join(" · ")}
            </p>
          )}
          <p className="muted" style={{ fontSize: 11.5 }}>{r.unit}. The latest market year is a USDA projection. {data.citation}</p>
        </>
      )}
    </Card>
  );
}
