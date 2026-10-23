"use client";

import { useState } from "react";
import { API_URL, api, type Account } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Check = { can_delete: boolean; blockers: string[]; workspaces_deleted: string[] };

export default function YourData({ account }: { account: Account }) {
  const [check, setCheck] = useState<Check | null>(null);
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const start = async () => {
    setError(null);
    try { setCheck(await api<Check>("/api/v1/account/deletion-check")); } catch (e) { setError(e); }
  };
  const remove = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/account/delete", { method: "POST", body: JSON.stringify({ password, code: code || undefined, confirm_email: confirm }) });
      window.location.href = "/login";
    } catch (err) { setError(err); setBusy(false); }
  };

  return (
    <Card title="Your data">
      <p style={{ marginTop: 0, fontSize: 13 }}>
        <a href={`${API_URL}/api/v1/account/export`}>Download my data</a>
        <span className="muted"> · your profile, workspaces, research questions, API keys (not the keys themselves), portfolios, alerts, agents, schedules and activity, as JSON.</span>
      </p>
      {!check && <button className="secondary" onClick={start}>Delete my account…</button>}
      {check && !check.can_delete && (
        <div className="note note-warn">Before deleting your account: {check.blockers.join("; ")}.</div>
      )}
      {check && check.can_delete && (
        <form onSubmit={remove} className="stack" style={{ gap: 8 }}>
          <div className="note note-warn">
            This permanently deletes your account{check.workspaces_deleted.length > 0 && <> and the workspace{check.workspaces_deleted.length > 1 ? "s" : ""} <strong>{check.workspaces_deleted.join(", ")}</strong> with all of its research, reports, share links, portfolios, alerts and agents</>}.
            {" "}Shared workspaces keep their content. This cannot be undone.
          </div>
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" autoComplete="current-password" aria-label="Password" required />
          {account.two_factor && <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="Authenticator or recovery code" autoComplete="one-time-code" aria-label="Two-factor code" required />}
          <input value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder={`Type ${account.user.email} to confirm`} aria-label="Type your email to confirm" required />
          <div className="row">
            <button type="submit" disabled={busy || confirm.trim().toLowerCase() !== account.user.email} style={{ background: "var(--down)" }}>{busy ? "Deleting…" : "Delete my account"}</button>
            <button type="button" className="secondary" onClick={() => setCheck(null)}>Cancel</button>
          </div>
        </form>
      )}
      <ErrorNote error={error} />
    </Card>
  );
}
