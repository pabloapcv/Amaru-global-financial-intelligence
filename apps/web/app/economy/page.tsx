"use client";

import { useEffect, useMemo, useState } from "react";
import Chart from "@/components/Chart";
import InstrumentDetail from "@/components/InstrumentDetail";
import { api, fmt, type ChartSpec, type Instrument } from "@/lib/api";
import { Card, ErrorNote, FreshnessBadge, PageHeader } from "@/components/ui";

type Indicator = { key: string; name: string; unit: string };
type Cell = { latest: number; year: number; avg_5obs: number; stale?: boolean };
type Row = { country: string; name: string } & Record<string, Cell | null | string>;
type Comparison = {
  result: { indicators: Record<string, { name: string; unit: string }>; rows: Row[]; reference_years: number[]; unavailable: unknown[] };
  chart: ChartSpec;
};

const GROUPS = [
  { key: "latam", label: "Latin America" },
  { key: "g20", label: "G20" },
];

function CountryComparison() {
  const [indicators, setIndicators] = useState<Indicator[]>([]);
  const [group, setGroup] = useState("latam");
  const [focus, setFocus] = useState("GDP_GROWTH");
  const [sortKey, setSortKey] = useState("GDP_GROWTH");
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState<unknown>(null);
  const columns = ["GDP_GROWTH", "INFLATION", "UNEMPLOYMENT", "CURRENT_ACCOUNT", "DEBT"];

  useEffect(() => {
    api<{ indicators: Indicator[] }>("/api/v1/countries").then((r) => setIndicators(r.indicators)).catch(setError);
  }, []);
  useEffect(() => {
    setData(null);
    const inds = [focus, ...columns.filter((c) => c !== focus)].join(",");
    api<Comparison>(`/api/v1/countries/compare?group=${group}&indicators=${inds}`).then(setData).catch(setError);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [group, focus]);

  const rows = useMemo(() => {
    if (!data) return [];
    const val = (r: Row) => {
      const c = r[sortKey] as Cell | null;
      return c && typeof c === "object" && !c.stale ? c.latest : -Infinity;
    };
    return [...data.result.rows].sort((a, b) => val(b) - val(a));
  }, [data, sortKey]);

  return (
    <Card
      title="Country comparison · World Bank WDI"
      actions={
        <div className="row">
          <div className="seg">{GROUPS.map((g) => <button key={g.key} className={g.key === group ? "on" : ""} onClick={() => setGroup(g.key)}>{g.label}</button>)}</div>
          <select value={focus} onChange={(e) => setFocus(e.target.value)} style={{ width: "auto" }}>
            {indicators.map((i) => <option key={i.key} value={i.key}>{i.name}</option>)}
          </select>
        </div>
      }
    >
      <ErrorNote error={error} />
      {!data ? <span className="muted">Loading…</span> : (
        <>
          <Chart spec={data.chart} height={260} />
          <table style={{ marginTop: 10 }}>
            <thead>
              <tr>
                <th>Country</th>
                {Object.entries(data.result.indicators).map(([k, ind]) => (
                  <th key={k} className="num clickable" style={{ cursor: "pointer" }} onClick={() => setSortKey(k)} title="Sort">
                    {ind.name}{sortKey === k ? " ↓" : ""}<div style={{ textTransform: "none", fontWeight: 400 }}>{ind.unit}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.country}>
                  <td>{r.name}</td>
                  {Object.keys(data.result.indicators).map((k) => {
                    const v = r[k] as Cell | null;
                    if (!v) return <td key={k} className="num"><span className="muted">n/a</span></td>;
                    return (
                      <td key={k} className="num" style={v.stale ? { opacity: 0.5 } : undefined} title={v.stale ? "Much older than peer data; excluded from ranking" : undefined}>
                        {fmt(v.latest, 1)} <span className={v.stale ? "down" : "muted"} style={{ fontSize: 11 }}>{v.year}{v.stale ? " ⚠" : ""}</span>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
            Latest available year per country (reference years {data.result.reference_years.join(", ")}). Annual data published with a lag; n/a means the World Bank has no observation; ⚠ marks values more than 3 years older than peers (excluded from sorting and rankings).
          </p>
        </>
      )}
    </Card>
  );
}

export default function Economy() {
  const [series, setSeries] = useState<Instrument[]>([]);
  const [selected, setSelected] = useState("US.CPI.HEADLINE");
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api<Instrument[]>("/api/v1/instruments").then((all) => setSeries(all.filter((i) => i.dataset_id.startsWith("US.")))).catch(setError);
  }, []);

  return (
    <>
      <PageHeader title="Global economy" subtitle="Country comparisons from World Bank WDI, plus monthly U.S. series with point-in-time vintages." />
      <ErrorNote error={error} />
      <div className="stack">
        <CountryComparison />
        <div className="split">
          <Card title="United States · monthly">
            <table>
              <tbody>
                {series.map((s) => (
                  <tr key={s.dataset_id} className={`clickable ${s.dataset_id === selected ? "selected" : ""}`} onClick={() => setSelected(s.dataset_id)}>
                    <td>{s.name}<div className="muted" style={{ fontSize: 11.5 }}>{s.frequency} · {s.unit}</div></td>
                    <td className="num"><FreshnessBadge value={s.freshness} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          <InstrumentDetail id={selected} defaultYears={10} />
        </div>
      </div>
    </>
  );
}
