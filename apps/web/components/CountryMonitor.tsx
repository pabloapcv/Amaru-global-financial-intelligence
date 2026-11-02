"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Years = { last_year: number; this_year: number; next_year: number };
type Row = {
  country: string; name: string;
  inflation: { month: string; yoy_pct: number; a_year_earlier: number | null } | null;
  real_exchange_rate: { month: string; change_12m_pct: number | null; vs_10y_average_pct: number } | null;
  credit_gap: { quarter: string; pp_of_gdp: number; above_warning: boolean } | null;
  real_house_prices: { quarter: string; change_12m_pct: number | null } | null;
  imf_forecasts: Record<string, Record<string, number | null>> | null;
};
type Monitor = { available: boolean; reason?: string; countries?: Row[]; forecast_years?: Years; credit_gap_warning_pp?: number; caveat?: string; not_loaded?: string[] };

const TITLE = "Country monitor";
const pct = (v: number | null | undefined, signed = false) => (v == null ? "n/a" : `${signed && v > 0 ? "+" : ""}${v.toFixed(1)}%`);
const Dated = ({ children, when }: { children: React.ReactNode; when?: string }) => (
  <>{children}{when && <div className="muted" style={{ fontSize: 11 }}>{when}</div>}</>
);

export default function CountryMonitor() {
  const [data, setData] = useState<Monitor | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { api<Monitor>("/api/v1/macro/countries/monitor").then(setData).catch(setError); }, []);

  if (error) return <Card title={TITLE}><ErrorNote error={error} /></Card>;
  if (!data) return <Card title={TITLE}><span className="muted">Loading…</span></Card>;
  if (!data.available) return <Card title={TITLE}><p className="muted" style={{ margin: 0 }}>Not available yet: {data.reason}. Run <code>atlas ingest</code>.</p></Card>;
  const y = data.forecast_years!;
  return (
    <Card title={TITLE}>
      <p style={{ marginTop: 0 }}>The latest monthly and quarterly readings for each country, beside the IMF&apos;s forecasts. Each figure shows its own date.</p>
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              <th>Country</th><th className="num">Inflation, y/y</th><th className="num">Real exchange rate, 12m</th>
              <th className="num">Credit gap, pp of GDP</th><th className="num">Real house prices, 12m</th>
              <th className="num">IMF growth {y.this_year}</th><th className="num">IMF growth {y.next_year}</th><th className="num">IMF inflation {y.next_year}</th>
            </tr>
          </thead>
          <tbody>
            {data.countries!.map((c) => {
              const g = c.imf_forecasts?.gdp_growth ?? {}, inf = c.imf_forecasts?.inflation ?? {};
              return (
                <tr key={c.country}>
                  <td>{c.name}</td>
                  <td className="num"><Dated when={c.inflation?.month}>{pct(c.inflation?.yoy_pct)}</Dated></td>
                  <td className="num"><Dated when={c.real_exchange_rate?.month}>{pct(c.real_exchange_rate?.change_12m_pct, true)}</Dated></td>
                  <td className={`num ${c.credit_gap?.above_warning ? "down" : ""}`}>
                    <Dated when={c.credit_gap?.quarter}>{c.credit_gap ? `${c.credit_gap.pp_of_gdp > 0 ? "+" : ""}${c.credit_gap.pp_of_gdp.toFixed(1)}` : "n/a"}</Dated>
                  </td>
                  <td className="num"><Dated when={c.real_house_prices?.quarter}>{pct(c.real_house_prices?.change_12m_pct, true)}</Dated></td>
                  <td className="num">{pct(g[String(y.this_year)])}</td>
                  <td className="num">{pct(g[String(y.next_year)])}</td>
                  <td className="num">{pct(inf[String(y.next_year)])}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
        A rising real exchange rate means a stronger currency after inflation. A credit gap above {data.credit_gap_warning_pp} points (in red) is the Basel III warning level for a credit boom.
        {data.not_loaded && data.not_loaded.length > 0 && ` Not loaded yet: ${data.not_loaded.join(", ")}.`} {data.caveat} Sources: IMF, BIS.
      </p>
    </Card>
  );
}
