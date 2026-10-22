"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { API_URL, api, fmt, type History } from "@/lib/api";
import { Card, Change, ErrorNote, FreshnessBadge } from "@/components/ui";

const RANGES = [1, 3, 5, 10, 25];

export default function InstrumentDetail({ id, defaultYears = 5 }: { id: string; defaultYears?: number }) {
  const [years, setYears] = useState(defaultYears);
  const [hist, setHist] = useState<History | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [watched, setWatched] = useState<boolean | null>(null);

  useEffect(() => {
    api<{ dataset_ids: string[] }>("/api/v1/watchlist").then((w) => setWatched(w.dataset_ids.includes(id))).catch(() => setWatched(null));
  }, [id]);
  const toggleWatch = async () => {
    try {
      const w = await api<{ dataset_ids: string[] }>(`/api/v1/watchlist/${id}`, { method: watched ? "DELETE" : "POST" });
      setWatched(w.dataset_ids.includes(id));
    } catch (e) { setError(e); }
  };

  useEffect(() => {
    setHist(null);
    setError(null);
    const path = id.startsWith("US.") ? "/api/v1/macro/series" : "/api/v1/markets/history";
    api<History>(`${path}?id=${id}&lookback_years=${years}`).then(setHist).catch(setError);
  }, [id, years]);

  if (error) return <ErrorNote error={error} />;
  if (!hist) return <Card><span className="muted">Loading…</span></Card>;
  const m = hist.meta;
  const p = hist.performance;
  const pctUnit = m.unit === "percent";
  return (
    <div className="stack">
      <Card
        title={m.name}
        actions={
          <div className="row" style={{ flexWrap: "nowrap" }}>
            {watched !== null && (
              <button className="secondary" onClick={toggleWatch} aria-pressed={watched} title={watched ? "Remove from the workspace watchlist" : "Add to the workspace watchlist (shown on Overview)"}>
                {watched ? "★ Watching" : "☆ Watch"}
              </button>
            )}
            <div className="seg">
              {RANGES.map((r) => (
                <button key={r} className={r === years ? "on" : ""} onClick={() => setYears(r)}>{r}Y</button>
              ))}
            </div>
          </div>
        }
      >
        <Chart spec={{ type: "line", title: m.name, unit: m.unit, series: [{ name: m.name, points: hist.points }] }} height={320} />
        <p className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>
          {m.unit} · {m.frequency} · {String(m.price_basis).replaceAll("_", " ")} · source{" "}
          {m.source_url ? <a href={m.source_url} target="_blank">{m.provider_symbol}</a> : "none"}
        </p>
      </Card>
      <div className="grid grid-2">
        <Card title="Performance">
          {p ? (
            <table>
              <tbody>
                {Object.entries(p.trailing as Record<string, any>).map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td className="num">{v ? (pctUnit ? <Change value={v.change_bp} unit="bp" /> : <Change value={v.return_pct} />) : "—"}</td>
                    <td className="num muted">{v?.from_date ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <span className="muted">Not enough data.</span>}
        </Card>
        <Card title="Data provenance">
          <dl className="kv">
            <dt>Last value</dt><dd>{fmt(p?.last_value, 4)} ({m.last_observation})</dd>
            <dt>Freshness</dt><dd><FreshnessBadge value={m.freshness} /> <span className="muted">{m.freshness_note}</span></dd>
            <dt>Ingested</dt><dd>{m.last_ingested_at ?? "—"}</dd>
            <dt>Checked</dt><dd>{m.last_checked_at ?? "—"}</dd>
            <dt>History</dt><dd>{m.first_observation} → {m.last_observation} ({m.n_observations.toLocaleString()} obs)</dd>
            {p?.annualized_vol_1y_pct != null && (<><dt>1Y vol</dt><dd>{fmt(p.annualized_vol_1y_pct)}%</dd></>)}
            {p?.max_drawdown_5y_pct != null && (<><dt>5Y max DD</dt><dd>{fmt(p.max_drawdown_5y_pct)}%</dd></>)}
            {p?.percentile_in_5y_range != null && (<><dt>5Y percentile</dt><dd>{fmt(p.percentile_in_5y_range, 1)}</dd></>)}
            <dt>Citation</dt><dd className="muted" style={{ fontSize: 12 }}>{m.citation ?? "—"}</dd>
            <dt>Export</dt><dd><a href={`${API_URL}/api/v1/datasets/${id}/export?format=csv`}>CSV</a> · <a href={`${API_URL}/api/v1/datasets/${id}/export?format=json`}>JSON</a> <span className="muted" style={{ fontSize: 12 }}>full history with provenance</span></dd>
          </dl>
        </Card>
      </div>
    </div>
  );
}
