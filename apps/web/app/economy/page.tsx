"use client";

import { useEffect, useMemo, useState } from "react";
import BusinessCycle from "@/components/BusinessCycle";
import CountryMonitor from "@/components/CountryMonitor";
import EnergyInventories from "@/components/EnergyInventories";
import FertilizerAffordability from "@/components/FertilizerAffordability";
import PolicyRates from "@/components/PolicyRates";
import Chart from "@/components/Chart";
import InstrumentDetail from "@/components/InstrumentDetail";
import TradeFlows from "@/components/TradeFlows";
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

type CalEvent = { date: string; end_date: string | null; kind: string; title: string; source: string; url: string | null; detail: { datasets?: { dataset_id: string; name: string }[] } };
type SeriesDue = { dataset_id: string; name: string; frequency: string; last_period: string; next_period_due: string | null; official_release_date: string | null };
type Calendar = { from: string; to: string; events: CalEvent[]; series_due: SeriesDue[]; sources: Record<string, string>; notes: string[] };

const SOURCE_NAMES: Record<string, string> = { fomc: "Federal Reserve Board", fred_releases: "FRED release calendar" };

function fmtDay(iso: string) {
  return new Date(iso + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function EconomicCalendar() {
  const [days, setDays] = useState(60);
  const [cal, setCal] = useState<Calendar | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    api<Calendar>(`/api/v1/calendar?days=${days}`).then(setCal).catch(setError);
  }, [days]);
  const fetched = cal && Object.entries(cal.sources).map(([s, at]) => `${SOURCE_NAMES[s] ?? s}, fetched ${at.replace("T", " ")} UTC`).join(" · ");

  return (
    <Card
      title="Economic calendar"
      actions={<div className="seg">{[30, 60, 120].map((d) => <button key={d} className={d === days ? "on" : ""} onClick={() => setDays(d)}>{d}d</button>)}</div>}
    >
      <ErrorNote error={error} />
      {cal && (
        <>
          {cal.events.length === 0 ? (
            <p className="muted">No scheduled events in the next {days} days.</p>
          ) : (
            <table>
              <tbody>
                {cal.events.map((e) => (
                  <tr key={`${e.source}-${e.date}-${e.title}`}>
                    <td style={{ whiteSpace: "nowrap" }}>{fmtDay(e.date)}{e.end_date && e.end_date !== e.date ? ` – ${fmtDay(e.end_date)}` : ""}</td>
                    <td>
                      {e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.title}</a> : e.title}
                      {e.detail.datasets && <div className="muted" style={{ fontSize: 11.5 }}>{e.detail.datasets.map((d) => d.name).join(" · ")}</div>}
                    </td>
                    <td className="num muted">{e.kind === "fomc_meeting" ? "policy" : "data"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {cal.series_due.length > 0 && (
            <>
              <h4 style={{ margin: "14px 0 6px" }}>Next U.S. data due</h4>
              <table>
                <tbody>
                  {cal.series_due.map((d) => (
                    <tr key={d.dataset_id}>
                      <td>{d.name}</td>
                      <td className="num">{d.next_period_due ?? "—"}</td>
                      <td className="num muted">{d.official_release_date ? fmtDay(d.official_release_date) : "date not listed"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {cal.notes.map((n) => <p key={n} className="muted" style={{ fontSize: 12, marginTop: 10 }}>{n}</p>)}
          {fetched && <p className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>Source: {fetched}</p>}
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
      <PageHeader title="Global economy" subtitle="Economic calendar (Federal Reserve), country data (IMF, BIS, World Bank WDI), bilateral trade (UN Comtrade), and monthly U.S. series with point-in-time vintages." />
      <ErrorNote error={error} />
      <div className="stack">
        <EconomicCalendar />
        <BusinessCycle />
        <PolicyRates />
        <CountryMonitor />
        <FertilizerAffordability />
        <EnergyInventories />
        <CountryComparison />
        <TradeFlows />
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
