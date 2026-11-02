"use client";

import Link from "next/link";
import { Fragment, useCallback, useEffect, useState } from "react";
import { API_URL, api } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type Schedule = { schedule_id: string; name: string; query: string; cadence: string; status: string; next_run_at: string; last_run_at: string | null; last_run_id: string | null;
  config: { newsletter?: "off" | "draft" | "send" } };

const NEWSLETTER_MODES: [string, string][] = [["off", "Not in the newsletter"], ["draft", "Draft a newsletter issue"], ["send", "Send as a newsletter issue"]];

function Schedules() {
  const [rows, setRows] = useState<Schedule[]>([]);
  const [name, setName] = useState("Weekly sugar brief");
  const [query, setQuery] = useState("What's happening with sugar, and what are the main drivers? Include positioning. Outlook for the next three months.");
  const [cadence, setCadence] = useState("weekly");
  const [template, setTemplate] = useState("research_report");
  const [newsletter, setNewsletter] = useState("off");
  const [hasNewsletter, setHasNewsletter] = useState(false);
  const [templates, setTemplates] = useState<{ key: string; name: string; default_query: string | null }[]>([]);
  useEffect(() => {
    api<typeof templates>("/api/v1/report-templates").then(setTemplates).catch(() => {});
    // Only owners and admins with a newsletter set up can feed it (others get 403 here and see no option).
    api<{ newsletter: unknown }>("/api/v1/newsletter").then((r) => setHasNewsletter(!!r.newsletter)).catch(() => {});
  }, []);
  const [error, setError] = useState<unknown>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => api<Schedule[]>("/api/v1/schedules").then(setRows).catch(setError), []);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    setError(null);
    try {
      await api("/api/v1/schedules", { method: "POST", body: JSON.stringify({ name, query, cadence, template, newsletter }) });
      load();
    } catch (e) { setError(e); }
  };
  const setMode = async (id: string, mode: string) => {
    setError(null);
    try { await api(`/api/v1/schedules/${id}`, { method: "PATCH", body: JSON.stringify({ newsletter: mode }) }); load(); } catch (e) { setError(e); }
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
            {hasNewsletter && (
              <select value={newsletter} onChange={(e) => setNewsletter(e.target.value)} aria-label="Newsletter" style={{ width: "auto" }}>
                {NEWSLETTER_MODES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
              </select>
            )}
            <button onClick={create}>Create schedule</button>
          </div>
          {hasNewsletter && newsletter === "send" && (
            <p className="muted" style={{ fontSize: 12 }}>Sent automatically only when the research passes verification; otherwise it waits as a draft and you get an email.</p>
          )}
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
              {hasNewsletter && (
                <select value={r.config?.newsletter ?? "off"} onChange={(e) => setMode(r.schedule_id, e.target.value)} aria-label={`Newsletter for ${r.name}`}
                  style={{ width: "auto", marginTop: 4, fontSize: 12 }}>
                  {NEWSLETTER_MODES.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
                </select>
              )}
            </li>
          ))}
          {rows.length === 0 && <li className="muted">No schedules yet.</li>}
        </ul>
      </div>
    </Card>
  );
}

type Share = { share_id: string; prefix: string; status: "active" | "expired" | "revoked"; created_at: string; expires_at: string; view_count: number; last_viewed_at: string | null };

function SharePanel({ reportId }: { reportId: string }) {
  const [shares, setShares] = useState<Share[]>([]);
  const [days, setDays] = useState(7);
  const [created, setCreated] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => api<Share[]>(`/api/v1/reports/${reportId}/shares`).then(setShares).catch(setError), [reportId]);
  useEffect(() => { load(); }, [load]);

  const create = async () => {
    setError(null);
    setCopied(false);
    try {
      const r = await api<{ path: string }>(`/api/v1/reports/${reportId}/shares`, { method: "POST", body: JSON.stringify({ days }) });
      setCreated(`${API_URL}${r.path}`);
      load();
    } catch (e) { setError(e); }
  };
  const copy = async () => {
    if (!created) return;
    try { await navigator.clipboard.writeText(created); setCopied(true); } catch { setCopied(false); }
  };
  const when = (iso: string | null) => (iso ? iso.replace("T", " ").slice(0, 16) : "never");

  return (
    <div style={{ padding: "6px 0 10px" }}>
      <div className="row" style={{ flexWrap: "wrap" }}>
        <label className="muted" htmlFor={`d-${reportId}`} style={{ fontSize: 12.5 }}>Read-only link valid for</label>
        <select id={`d-${reportId}`} value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ width: "auto" }}>
          {[1, 7, 30, 90].map((d) => <option key={d} value={d}>{d} day{d > 1 ? "s" : ""}</option>)}
        </select>
        <button onClick={create}>Create link</button>
      </div>
      {created && (
        <div className="note" style={{ marginTop: 8 }}>
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <input readOnly value={created} onFocus={(e) => e.target.select()} style={{ minWidth: 0, flex: 1 }} aria-label="Share link" />
            <button className="secondary" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
          </div>
          <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>Copy it now: Amaru stores only a hash and cannot show this link again. Anyone with it can view this report (not your workspace) until it expires or you revoke it.</p>
        </div>
      )}
      <ErrorNote error={error} />
      {shares.length > 0 && (
        <ul className="list-tight" style={{ marginTop: 8 }}>
          {shares.map((sh) => (
            <li key={sh.share_id} className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
              <span style={{ fontSize: 12.5 }}>
                <code>{sh.prefix}…</code> · <span className={sh.status === "active" ? "up" : "muted"}>{sh.status}</span>
                <span className="muted"> · expires {when(sh.expires_at)} · {sh.view_count} view{sh.view_count === 1 ? "" : "s"}, last {when(sh.last_viewed_at)}</span>
              </span>
              {sh.status === "active" && <button className="secondary" onClick={() => api(`/api/v1/shares/${sh.share_id}`, { method: "DELETE" }).then(load).catch(setError)}>Revoke</button>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type ReportRow = { report_id: string; run_id: string; title: string; query: string; version: number; formats: string[]; created_at: string };

export default function Reports() {
  const [rows, setRows] = useState<ReportRow[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [sharing, setSharing] = useState<string | null>(null);

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
              <Fragment key={r.report_id}>
              <tr>
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
                    <button className="secondary" onClick={() => setSharing(sharing === r.report_id ? null : r.report_id)} aria-expanded={sharing === r.report_id}>Share</button>
                  </div>
                </td>
              </tr>
              {sharing === r.report_id && <tr><td colSpan={4}><SharePanel reportId={r.report_id} /></td></tr>}
              </Fragment>
            ))}
            {rows.length === 0 && <tr><td colSpan={4} className="muted">No reports yet.</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  );
}
