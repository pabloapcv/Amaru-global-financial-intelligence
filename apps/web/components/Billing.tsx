"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Plan = { key: string; available: boolean; limits: Record<string, number | null> };
type Summary = { configured: boolean; plan: string; status: string | null; has_customer: boolean; can_manage: boolean; plans: Plan[] };

const NAMES: Record<string, string> = { explorer: "Explorer", investor: "Investor", research_pro: "Research Pro", pro: "Pro" };
const SHOWN: [string, string][] = [["seats", "Seats"], ["research_per_day", "Research runs / day"], ["llm_research_per_day", "Claude runs / day"],
  ["llm_budget_usd_per_month", "Claude budget / month ($)"], ["agents", "Agents"], ["api_keys", "API keys"], ["requests_per_minute", "API requests / minute"]];

export default function Billing() {
  const params = useSearchParams();
  const [b, setB] = useState<Summary | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const returned = params.get("billing");

  useEffect(() => {
    const load = () => api<Summary>("/api/v1/billing").then(setB).catch(setError);
    load();
    if (returned === "success") { const t = setTimeout(load, 4000); return () => clearTimeout(t); }  // webhook lands shortly after
  }, [returned]);
  if (!b) return null;

  const go = async (path: string, body?: object) => {
    setBusy(path);
    setError(null);
    try {
      const r = await api<{ url: string }>(path, { method: "POST", ...(body ? { body: JSON.stringify(body) } : {}) });
      window.location.href = r.url;
    } catch (e) { setError(e); setBusy(null); }
  };

  return (
    <Card title={`Plan · ${NAMES[b.plan] ?? b.plan}${b.status && b.status !== "active" ? ` · ${b.status.replace("_", " ")}` : ""}`}
      actions={b.configured && b.can_manage && b.has_customer && (
        <button className="secondary" disabled={!!busy} onClick={() => go("/api/v1/billing/portal")}>Manage billing</button>
      )}>
      {returned === "success" && <div className="note note-ok" style={{ marginBottom: 10 }}>Payment received. Your plan updates as soon as the payment provider confirms it.</div>}
      {returned === "cancelled" && <div className="note" style={{ marginBottom: 10 }}>Checkout cancelled; nothing was charged.</div>}
      {b.status === "past_due" && <div className="note note-warn" style={{ marginBottom: 10 }}>The last payment failed. Update your payment method under Manage billing to keep this plan.</div>}
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead><tr><th></th>{b.plans.map((p) => <th key={p.key} className="num">{NAMES[p.key] ?? p.key}{p.key === b.plan && " ✓"}</th>)}</tr></thead>
          <tbody>
            {SHOWN.map(([k, label]) => (
              <tr key={k}><td className="muted">{label}</td>{b.plans.map((p) => <td key={p.key} className="num">{p.limits[k] ?? "∞"}</td>)}</tr>
            ))}
            {b.configured && b.can_manage && (
              <tr><td></td>{b.plans.map((p) => (
                <td key={p.key} className="num">
                  {p.key !== b.plan && p.available && !b.has_customer && <button disabled={!!busy} onClick={() => go("/api/v1/billing/checkout", { plan: p.key })}>Choose</button>}
                </td>
              ))}</tr>
            )}
          </tbody>
        </table>
      </div>
      {b.configured && b.has_customer && b.can_manage && <p className="muted" style={{ fontSize: 12 }}>Change or cancel your subscription under Manage billing.</p>}
      {!b.configured && <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>Self-service billing is not set up on this deployment; an operator changes plans (<code>atlas set-plan</code>).</p>}
      {b.configured && !b.can_manage && <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>Only the workspace owner can change the plan.</p>}
      <ErrorNote error={error} />
    </Card>
  );
}
