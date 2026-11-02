"use client";

import { useCallback, useEffect, useState } from "react";
import { API_URL, api, type Freshness } from "@/lib/api";
import { Card, ErrorNote, FreshnessBadge, PageHeader } from "@/components/ui";

const LICENCES: Record<string, [string, string]> = {
  public_domain: ["Public domain", "current"], cc_by: ["CC BY 4.0", "current"], check_terms: ["Check terms", "delayed"],
  third_party_restricted: ["Restricted", "stale"],
};
const PROVIDERS: Record<string, string> = { fred: "FRED", cftc: "CFTC", worldbank: "WB", comtrade: "Comtrade", usda_psd: "USDA", bis: "BIS", worldbank_cmo: "WB Pink Sheet", eia: "EIA", imf: "IMF", imf_weo: "IMF WEO" };

type Status = {
  dataset_id: string; name: string; kind: string; asset_class: string; frequency: string; provider_id: string | null; provider_symbol: string | null;
  licence: string | null;
  observation_date: string | null; ingested_at: string | null; checked_at: string | null; n_observations: number;
  freshness: Freshness; freshness_note: string;
};
type Pipelines = {
  success_rate_30d: number | null;
  runs_30d: number;
  recent_runs: { execution_id: string; dataset_id: string; status: string; started_at: string; rows_fetched: number; rows_inserted: number; rows_revised: number; findings: { check: string; severity: string; message: string }[]; error: string | null }[];
};

export default function DataExplorer() {
  const [status, setStatus] = useState<Status[]>([]);
  const [pipes, setPipes] = useState<Pipelines | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [kind, setKind] = useState("market");
  const [filter, setFilter] = useState("");
  const shown = status.filter((s) =>
    (kind === "all" || (kind === "country" ? /^(WB|BIS)\./.test(s.dataset_id) : s.kind === kind && !/^(WB|BIS)\./.test(s.dataset_id))) &&
    (!filter || `${s.name} ${s.dataset_id}`.toLowerCase().includes(filter.toLowerCase())));

  const load = useCallback(() => {
    Promise.all([api<Status[]>("/api/v1/data/status"), api<Pipelines>("/api/v1/pipelines/status?limit=40")])
      .then(([s, p]) => { setStatus(s); setPipes(p); })
      .catch(setError);
  }, []);
  useEffect(() => { load(); }, [load]);

  const refresh = async () => {
    await api("/api/v1/pipelines/run", { method: "POST", body: JSON.stringify({}) });
    setMsg("Refresh started for all datasets. Reload in a minute to see new runs.");
  };

  return (
    <>
      <PageHeader
        title="Data Explorer"
        subtitle="Every dataset shows its observation date, ingestion time and freshness separately."
        actions={<div className="row"><button className="secondary" onClick={load}>Reload</button><button onClick={refresh}>Refresh all</button></div>}
      />
      <ErrorNote error={error} />
      {msg && <div className="note" style={{ marginBottom: 14 }}>{msg}</div>}
      <Card
        title={`Datasets (${shown.length} of ${status.length})`}
        actions={
          <div className="row">
            <div className="seg">
              {[["market", "Markets"], ["macro", "U.S. macro"], ["country", "Countries"], ["positioning", "Positioning"], ["trade", "Trade"], ["fundamentals", "USDA"], ["all", "All"]].map(([k, l]) => (
                <button key={k} className={k === kind ? "on" : ""} onClick={() => setKind(k)}>{l}</button>
              ))}
            </div>
            <input placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: 160 }} />
          </div>
        }
      >
        <div style={{ overflowX: "auto" }}>
        <table>
          <thead><tr><th>Dataset</th><th>Source</th><th>Freq.</th><th>Observation</th><th>Ingested</th><th className="num">Obs.</th><th>Freshness</th><th>Licence</th><th className="num">Export</th></tr></thead>
          <tbody>
            {shown.map((s) => (
              <tr key={s.dataset_id}>
                <td>{s.name}<div className="muted mono">{s.dataset_id}</div></td>
                <td className="mono">{s.provider_symbol ? `${PROVIDERS[s.provider_id ?? ""] ?? s.provider_id}:${s.provider_symbol}` : "—"}</td>
                <td>{s.frequency}</td>
                <td>{s.observation_date ?? "—"}</td>
                <td className="muted">{s.ingested_at?.replace("T", " ").slice(0, 16) ?? "—"}</td>
                <td className="num">{s.n_observations.toLocaleString()}</td>
                <td title={s.freshness_note}><FreshnessBadge value={s.freshness} /></td>
                <td>{s.licence ? <span className={`badge badge-${LICENCES[s.licence]?.[1] ?? "no_data"}`}>{LICENCES[s.licence]?.[0] ?? s.licence}</span> : <span className="muted">—</span>}</td>
                <td className="num">{s.n_observations > 0 ? <a href={`${API_URL}/api/v1/datasets/${s.dataset_id}/export?format=csv`}>CSV</a> : <span className="muted">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </Card>
      <div style={{ height: 14 }} />
      <Card title={`Pipeline runs · 30-day success rate ${pipes?.success_rate_30d != null ? (pipes.success_rate_30d * 100).toFixed(1) + "%" : "—"} (${pipes?.runs_30d ?? 0} runs)`}>
        <table>
          <thead><tr><th>Started</th><th>Dataset</th><th>Status</th><th className="num">Fetched</th><th className="num">New</th><th className="num">Revised</th><th>Quality findings</th></tr></thead>
          <tbody>
            {pipes?.recent_runs.map((r) => (
              <tr key={r.execution_id}>
                <td className="muted">{r.started_at.replace("T", " ").slice(0, 19)}</td>
                <td className="mono">{r.dataset_id}</td>
                <td className={r.status === "success" ? "up" : "down"}>{r.status}</td>
                <td className="num">{r.rows_fetched}</td>
                <td className="num">{r.rows_inserted}</td>
                <td className="num">{r.rows_revised}</td>
                <td style={{ fontSize: 12 }}>
                  {r.error && <div className="down">{r.error}</div>}
                  {r.findings.filter((f) => f.severity !== "info").map((f, i) => <div key={i} className={f.severity === "error" ? "down" : "muted"}>{f.check}: {f.message}</div>)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </>
  );
}
