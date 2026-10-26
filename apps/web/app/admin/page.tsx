"use client";

import { useCallback, useEffect, useState } from "react";
import { api, fmt } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type Overview = { workspaces: number; users: number; research_runs_7d: number; paying_workspaces: number; by_plan: Record<string, number>;
  claude_spend_month_usd: number; suspended: number; pipeline_success_rate_30d: number | null; feedback_open: number; satisfaction_30d: number | null };
type Fb = { feedback_id: string; rating: string; reason_labels: string[]; comment: string; question: string; title: string | null; engine: string | null;
  verified: boolean | null; reader: string | null; workspace: string | null; status: string; operator_note: string | null; updated_at: string };
type Row = { workspace_id: string; name: string; plan: string; created_at: string; members: number; owner: string | null; research_runs_30d: number;
  last_activity_at: string | null; billing_status: string | null; suspended_at: string | null; requires_2fa: boolean; claude_spend_month_usd: number };

const PLANS = ["explorer", "investor", "research_pro", "pro"];

export default function Admin() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [filter, setFilter] = useState("");
  const [fb, setFb] = useState<Fb[]>([]);
  const [fbStatus, setFbStatus] = useState<"open" | "resolved" | "all">("open");
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => {
    Promise.all([api<Overview>("/api/v1/admin/overview"), api<Row[]>("/api/v1/admin/workspaces")])
      .then(([o, r]) => { setOv(o); setRows(r); setError(null); }).catch(setError);
  }, []);
  useEffect(() => { load(); }, [load]);
  const loadFb = useCallback(() => { api<{ feedback: Fb[] }>(`/api/v1/admin/feedback?status=${fbStatus}`).then((r) => setFb(r.feedback)).catch(setError); }, [fbStatus]);
  useEffect(() => { loadFb(); }, [loadFb]);
  const resolveFb = async (f: Fb, status: "open" | "resolved") => {
    const note = status === "resolved" ? window.prompt("Reply to the reader (optional; shown under their answer):", f.operator_note ?? "") : null;
    if (status === "resolved" && note === null) return;
    try { await api(`/api/v1/admin/feedback/${f.feedback_id}`, { method: "PATCH", body: JSON.stringify({ status, note }) }); loadFb(); load(); } catch (e) { setError(e); }
  };

  const update = async (id: string, body: Record<string, unknown>) => {
    try { await api(`/api/v1/admin/workspaces/${id}`, { method: "PATCH", body: JSON.stringify(body) }); load(); } catch (e) { setError(e); }
  };
  const shown = rows.filter((r) => !filter || `${r.name} ${r.owner ?? ""}`.toLowerCase().includes(filter.toLowerCase()));
  const day = (iso: string | null) => (iso ? iso.slice(0, 10) : "—");

  return (
    <>
      <PageHeader title="Operator console" subtitle="Every workspace's plan, usage and status, plus the feedback readers chose to send. Research content is otherwise never shown here. Every change is audited." />
      <ErrorNote error={error} />
      {ov && (
        <div className="grid grid-4" style={{ marginBottom: 14 }}>
          <Card title="Workspaces"><div className="quote"><div className="value">{ov.workspaces}</div><div className="sub">{ov.users} users · {ov.paying_workspaces} paying{ov.suspended ? ` · ${ov.suspended} suspended` : ""}</div></div></Card>
          <Card title="Research · 7 days"><div className="quote"><div className="value">{ov.research_runs_7d}</div><div className="sub">runs across all workspaces</div></div></Card>
          <Card title="Claude spend · month"><div className="quote"><div className="value">${fmt(ov.claude_spend_month_usd, 2)}</div><div className="sub">all workspaces</div></div></Card>
          <Card title="Feedback · 30 days"><div className="quote"><div className="value">{ov.satisfaction_30d != null ? `${fmt(ov.satisfaction_30d * 100, 0)}%` : "—"}</div><div className="sub">answers rated right · {ov.feedback_open} open</div></div></Card>
          <Card title="Pipelines · 30 days"><div className="quote"><div className="value">{ov.pipeline_success_rate_30d != null ? `${fmt(ov.pipeline_success_rate_30d * 100, 2)}%` : "—"}</div><div className="sub">successful runs</div></div></Card>
        </div>
      )}
      <Card title="Workspaces" actions={<input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by name or owner" aria-label="Filter workspaces" style={{ width: 220 }} />}>
        <div style={{ overflowX: "auto" }}>
          <table>
            <thead><tr><th>Workspace</th><th>Plan</th><th className="num">Members</th><th className="num">Runs 30d</th><th className="num">Claude $ (month)</th><th>Billing</th><th>Last activity</th><th className="num">Status</th></tr></thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.workspace_id}>
                  <td>{r.name}<div className="muted" style={{ fontSize: 11.5 }}>{r.owner ?? "no owner"} · since {day(r.created_at)}{r.requires_2fa ? " · 2FA required" : ""}</div></td>
                  <td>
                    <select value={r.plan} aria-label={`Plan for ${r.name}`} style={{ width: "auto" }}
                      onChange={(e) => window.confirm(`Change ${r.name} to ${e.target.value}?`) && update(r.workspace_id, { plan: e.target.value })}>
                      {PLANS.map((p) => <option key={p} value={p}>{p.replace("_", " ")}</option>)}
                    </select>
                  </td>
                  <td className="num">{r.members}</td>
                  <td className="num">{r.research_runs_30d}</td>
                  <td className="num">{fmt(r.claude_spend_month_usd, 2)}</td>
                  <td className="muted">{r.billing_status ?? "—"}</td>
                  <td className="muted">{day(r.last_activity_at)}</td>
                  <td className="num">
                    {r.suspended_at
                      ? <button className="secondary" onClick={() => update(r.workspace_id, { suspended: false })}>Reinstate</button>
                      : <button className="secondary" onClick={() => { const reason = window.prompt(`Suspend ${r.name}? Its members and API keys lose access until reinstated. Reason:`); if (reason !== null) update(r.workspace_id, { suspended: true, reason }); }}>Suspend</button>}
                    {r.suspended_at && <div className="muted" style={{ fontSize: 11 }}>suspended {day(r.suspended_at)}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <div style={{ marginTop: 14 }}>
        <Card title="Answer feedback" actions={
          <select value={fbStatus} onChange={(e) => setFbStatus(e.target.value as typeof fbStatus)} aria-label="Feedback status" style={{ width: "auto" }}>
            <option value="open">Open</option><option value="resolved">Resolved</option><option value="all">All</option>
          </select>}>
          {fb.length === 0 ? <p className="muted" style={{ margin: 0 }}>Nothing here.</p> : (
            <div style={{ overflowX: "auto" }}>
              <table>
                <thead><tr><th>Question</th><th>Rating</th><th>What was wrong</th><th>From</th><th className="num"></th></tr></thead>
                <tbody>{fb.map((f) => (
                  <tr key={f.feedback_id}>
                    <td style={{ maxWidth: 360 }}>{f.question}<div className="muted" style={{ fontSize: 11.5 }}>{f.title ?? ""}{f.engine ? ` · ${f.engine}` : ""}{f.verified === false ? " · failed verification" : ""} · {f.updated_at.slice(0, 10)}</div></td>
                    <td>{f.rating === "up" ? "Right" : "Wrong"}</td>
                    <td style={{ maxWidth: 320 }}>{f.reason_labels.join(", ")}{f.comment && <div className="muted" style={{ fontSize: 12, whiteSpace: "pre-wrap" }}>{f.comment}</div>}
                      {f.operator_note && <div style={{ fontSize: 12 }}>Reply: {f.operator_note}</div>}</td>
                    <td className="muted" style={{ fontSize: 12 }}>{f.reader ?? "deleted user"}<div>{f.workspace}</div></td>
                    <td className="num">{f.status === "open"
                      ? <button className="secondary" onClick={() => resolveFb(f, "resolved")}>Resolve</button>
                      : <button className="secondary" onClick={() => resolveFb(f, "open")}>Reopen</button>}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
