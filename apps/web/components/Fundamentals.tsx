"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, fmt, type ChartSpec } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Row = { market_year: string; status: string; production: number | null; consumption: number | null; exports: number | null; ending_stocks: number | null; stocks_to_use_pct: number | null };
type Share = { country: string; value: number; share_pct: number };
type PriceModel = {
  status?: string; reason?: string; price_unit: string; stocks_basis: string; stocks_basis_note?: string; n_years: number;
  first_market_year: string; last_market_year: string; slope_pct_per_pp: number; r_squared: number; significance: string;
  has_signal: boolean; band_pct: number; projection_market_year: string; projected_stocks_to_use_pct: number;
  implied_price: number; latest_price: number; latest_price_date: string; latest_vs_implied_pct: number; assessment: string;
  extrapolated: boolean; points: { market_year: string; stocks_to_use_pct: number; real_price: number }[];
  [k: string]: unknown;
};

function PriceScatter({ m }: { m: PriceModel }) {
  const W = 520, H = 200, P = { l: 48, r: 12, t: 10, b: 28 };
  const xs = [...m.points.map((p) => p.stocks_to_use_pct), m.projected_stocks_to_use_pct];
  const band = (m[`implied_price_band_${m.band_pct}`] as number[] | undefined) ?? [m.implied_price, m.implied_price];
  const ys = [...m.points.map((p) => p.real_price), m.latest_price, ...(m.has_signal ? band : [])];
  const [x0, x1] = [Math.min(...xs) * 0.95, Math.max(...xs) * 1.05];
  const [y0, y1] = [Math.min(...ys) * 0.9, Math.max(...ys) * 1.05];
  const sx = (v: number) => P.l + ((v - x0) / (x1 - x0)) * (W - P.l - P.r);
  const sy = (v: number) => H - P.b - ((v - y0) / (y1 - y0)) * (H - P.t - P.b);
  // Fitted curve: log-linear, anchored at the implied price for the projection.
  const b = Math.log(1 + m.slope_pct_per_pp / 100);
  const curve = Array.from({ length: 30 }, (_, i) => x0 + ((x1 - x0) * i) / 29)
    .map((x) => `${sx(x).toFixed(1)},${sy(m.implied_price * Math.exp(b * (x - m.projected_stocks_to_use_pct))).toFixed(1)}`).join(" ");
  const ticks = (a: number, z: number) => [0, 0.5, 1].map((f) => a + (z - a) * f);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Real price versus stocks-to-use by market year" style={{ display: "block" }}>
      {ticks(y0, y1).map((t) => <g key={t}><line x1={P.l} x2={W - P.r} y1={sy(t)} y2={sy(t)} stroke="var(--grid)" /><text x={P.l - 6} y={sy(t) + 4} fontSize="10" textAnchor="end" fill="var(--muted)">{t.toFixed(0)}</text></g>)}
      {ticks(x0, x1).map((t) => <text key={t} x={sx(t)} y={H - 8} fontSize="10" textAnchor="middle" fill="var(--muted)">{t.toFixed(0)}%</text>)}
      {m.has_signal && <polyline points={curve} fill="none" stroke="var(--accent)" strokeWidth="1.5" opacity="0.8" />}
      {m.points.map((p) => <circle key={p.market_year} cx={sx(p.stocks_to_use_pct)} cy={sy(p.real_price)} r="3" fill="var(--muted)" opacity="0.7"><title>{`${p.market_year}: ${p.stocks_to_use_pct}% · ${p.real_price}`}</title></circle>)}
      {m.has_signal && <line x1={sx(m.projected_stocks_to_use_pct)} x2={sx(m.projected_stocks_to_use_pct)} y1={sy(band[0])} y2={sy(band[1])} stroke="var(--accent)" strokeWidth="6" opacity="0.25" />}
      <circle cx={sx(m.projected_stocks_to_use_pct)} cy={sy(m.latest_price)} r="5" fill="var(--warn)"><title>{`Latest price ${m.latest_price} at projected ${m.projection_market_year} stocks-to-use`}</title></circle>
    </svg>
  );
}

type Result = {
  commodity: string; geography: string; unit: string; usda_release: string; balance: Row[];
  change_vs_prior_year: Record<string, number | null>;
  stocks_to_use: { latest_pct: number | null; mean_pct: number | null; percentile: number | null; history_years: number };
  top_producers?: Share[]; top_exporters?: Share[];
  price_vs_stocks_to_use?: PriceModel;
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
          {r.price_vs_stocks_to_use?.points && (() => {
            const m = r.price_vs_stocks_to_use;
            return (
              <div style={{ marginTop: 12 }}>
                <h4 style={{ margin: "0 0 4px" }}>Price versus stocks-to-use</h4>
                <PriceScatter m={m} />
                <p className="muted" style={{ fontSize: 12.5, margin: "4px 0 0" }}>
                  {m.n_years} market years ({m.first_market_year}–{m.last_market_year}), {m.stocks_basis}; real {m.price_unit}. R² {fmt(m.r_squared, 2)}, {m.significance}.{" "}
                  {m.has_signal
                    ? <>At the projected {fmt(m.projected_stocks_to_use_pct, 1)}% for {m.projection_market_year} the fit implies {fmt(m.implied_price, 2)}; the latest price ({fmt(m.latest_price, 2)}, <span style={{ color: "var(--warn)" }}>●</span>) is {signed(m.latest_vs_implied_pct)}: {m.assessment}.</>
                    : <>Too weak to imply a price.</>}
                  {" "}Association, not a forecast.{m.stocks_basis_note ? ` ${m.stocks_basis_note}` : ""}
                </p>
              </div>
            );
          })()}
          {r.price_vs_stocks_to_use?.status === "unavailable" && <p className="muted" style={{ fontSize: 12 }}>Price model unavailable: {r.price_vs_stocks_to_use.reason}</p>}
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
