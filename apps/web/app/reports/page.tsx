"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { API_URL, api } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type Schedule = { schedule_id: string; name: string; query: string; cadence: string; status: string; next_run_at: string; last_run_at: string | null; last_run_id: string | null };

function Schedules() {
  const [rows, setRows] = useState<Schedule[]>([]);
  const [name, setName] = useState("Weekly sugar brief");
  const [query, setQuery] = useState("What's happening with sugar, and what are the main drivers? Include positioning. Outlook for the next three months.");
  const [cadence, setCadence] = useState("weekly");
  const [template, setTemplate] = useState("research_report");
  const [templates, setTemplates] = useState<{ key: string; name: string; default_query: string | null }[]>([]);
  useEffect(() => {
    api<typeof templates>("/api/v1/report-templates").then(setTemplates).catch(() => {});
  }, []);
  const [error, setError] = useState<unknown>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => api<Schedule[]>("/api/v1/schedules").then(setRows).catch(setError), []);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    setError(null);
    try {
      await api("/api/v1/schedules", { method: "POST", body: JSON.stringify({ name, query, cadence, template }) });
      load();
    } catch (e) { setError(e); }
  };
  const runNow = async (id: string) => {
    const r = await api<{ run_id: string }>(`/api/v1/schedules/${id}/run`, { method: "POST" });
    setMsg(`Started run ${r.run_id.slice(0, 8)}. It appears in the reports list when complete.`);
    load();
  };

  return (
    <Card title="Scheduled reports">
      <div className="grid grid-2">
        <div>
          <div className="field"><label>Name</label><input value={name} onChange={(e) => setName(e.target.value)} /></div>
          <div className="field">
            <label>Report type</label>
            <select value={template} onChange={(e) => {
              setTemplate(e.target.value);
              const t = templates.find((x) => x.key === e.target.value);
              if (t?.default_query) { setQuery(t.default_query); setName(t.name); }
            }}>
              {templates.map((t) => <option key={t.key} value={t.key}>{t.name}</option>)}
            </select>
          </div>
          <div className="field"><label>Research question</label><textarea value={query} onChange={(e) => setQuery(e.target.value)} /></div>
          <div className="row" style={{ marginTop: 10 }}>
            <select value={cadence} onChange={(e) => setCadence(e.target.value)} style={{ width: 140 }}>
              <option value="daily">Daily</option><option value="weekly">Weekly (Mon)</option><option value="monthly">Monthly (1st)</option>
            </select>
            <button onClick={create}>Create schedule</button>
          </div>
          <p className="muted" style={{ fontSize: 12 }}>Runs at 11:00 UTC after the data refresh, when the scheduler is on (<code>atlas schedule</code> or <code>ATLAS_SCHEDULER=on</code>).</p>
          <ErrorNote error={error} />
          {msg && <div className="note">{msg}</div>}
        </div>
        <ul className="list-tight">
          {rows.map((r) => (
            <li key={r.schedule_id}>
              <div className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap" }}>
                <strong>{r.name}</strong>
                <div className="row" style={{ flexWrap: "nowrap" }}>
                  <button className="secondary" onClick={() => runNow(r.schedule_id)}>Run now</button>
                  <button className="secondary" onClick={() => api(`/api/v1/schedules/${r.schedule_id}`, { method: "DELETE" }).then(load)}>Remove</button>
                </div>
              </div>
              <div className="muted" style={{ fontSize: 12 }}>{r.cadence} · next {r.next_run_at.replace("T", " ").slice(0, 16)} UTC{r.last_run_id && <> · <Link href={`/research?run=${r.last_run_id}`}>last run</Link></>}</div>
              <div style={{ fontSize: 12.5 }}>{r.query}</div>
            </li>
          ))}
          {rows.length === 0 && <li className="muted">No schedules yet.</li>}
        </ul>
      </div>
    </Card>
  );
}

type ReportRow = { report_id: string; run_id: string; title: string; query: string; version: number; formats: string[]; created_at: string };

export default function Reports() {
  const [rows, setRows] = useState<ReportRow[]>([]);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api<ReportRow[]>("/api/v1/reports").then(setRows).catch(setError);
  }, []);

  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="Versioned research outputs. HTML includes a print stylesheet for PDF; Excel carries the evidence, sources and chart data."
        actions={<Link href="/research"><button>New research</button></Link>}
      />
      <ErrorNote error={error} />
      <div style={{ marginBottom: 14 }}><Schedules /></div>
      <Card title="Report library">
        <table>
          <thead><tr><th>Report</th><th>Version</th><th>Created</th><th className="num">Downloads</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.report_id}>
                <td>
                  <Link href={`/research?run=${r.run_id}`}>{r.title}</Link>
                  <div className="muted" style={{ fontSize: 12 }}>{r.query}</div>
                </td>
                <td>v{r.version}</td>
                <td className="muted">{r.created_at.replace("T", " ").slice(0, 16)}</td>
                <td className="num">
                  <div className="row" style={{ justifyContent: "flex-end" }}>
                    {r.formats.includes("html") && <a href={`${API_URL}/api/v1/reports/${r.report_id}/download?format=html&inline=true`} target="_blank">HTML / PDF</a>}
                    {r.formats.includes("xlsx") && <a href={`${API_URL}/api/v1/reports/${r.report_id}/download?format=xlsx`}>Excel</a>}
                  </div>
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={4} className="muted">No reports yet.</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  );
}
