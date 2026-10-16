"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Account } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type Key = { key_id: string; name: string; prefix: string; created_at: string; last_used_at: string | null };

const LABELS: Record<string, string> = {
  research_per_day: "Research runs (24h)",
  llm_research_per_day: "Claude research runs (24h)",
  schedules: "Scheduled reports",
  alerts: "Alerts",
  api_keys: "API keys",
};

export default function AccountPage() {
  const [account, setAccount] = useState<Account | null>(null);
  const [keys, setKeys] = useState<Key[]>([]);
  const [keyName, setKeyName] = useState("Notebook");
  const [newKey, setNewKey] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(() => {
    api<Account>("/api/v1/account").then(setAccount).catch(setError);
    api<Key[]>("/api/v1/api-keys").then(setKeys).catch(setError);
  }, []);
  useEffect(() => { load(); }, [load]);

  const createKey = async () => {
    setError(null);
    try {
      const k = await api<{ key: string }>("/api/v1/api-keys", { method: "POST", body: JSON.stringify({ name: keyName }) });
      setNewKey(k.key);
      load();
    } catch (e) { setError(e); }
  };
  const logout = async () => {
    await api("/api/v1/auth/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/login";
  };

  return (
    <>
      <PageHeader title="Account" subtitle="Workspace, plan usage and API access." actions={<button className="secondary" onClick={logout}>Sign out</button>} />
      <ErrorNote error={error} />
      {account && (
        <div className="grid grid-2">
          <Card title="Profile">
            <dl className="kv">
              <dt>Name</dt><dd>{account.user.name || "—"}</dd>
              <dt>Email</dt><dd>{account.user.email}</dd>
              <dt>Workspace</dt><dd>{account.workspace?.name}</dd>
              <dt>Role</dt><dd>{account.role}</dd>
              <dt>Plan</dt><dd>{account.workspace?.plan.replace("_", " ")}</dd>
            </dl>
          </Card>
          <Card title="Usage">
            <table>
              <tbody>
                {Object.entries(LABELS).map(([k, label]) => {
                  const used = account.usage[k] ?? 0;
                  const limit = account.limits[k];
                  const pct = limit ? Math.min(100, (used / limit) * 100) : 0;
                  return (
                    <tr key={k}>
                      <td>{label}</td>
                      <td style={{ width: "40%" }}>
                        <div style={{ height: 6, background: "var(--panel-2)", borderRadius: 3 }}>
                          <div style={{ width: `${pct}%`, height: 6, borderRadius: 3, background: pct >= 100 ? "var(--down)" : "var(--accent)" }} />
                        </div>
                      </td>
                      <td className="num" style={{ whiteSpace: "nowrap" }}>{used} / {limit ?? "∞"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>
        </div>
      )}
      <div style={{ height: 14 }} />
      <Card title="API keys">
        <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
          Use a key from scripts and notebooks: <code>Authorization: Bearer atk_…</code>. Keys act on this workspace and are shown only once.
        </p>
        <div className="row">
          <input value={keyName} onChange={(e) => setKeyName(e.target.value)} style={{ maxWidth: 260 }} aria-label="Key name" />
          <button onClick={createKey}>Create key</button>
        </div>
        {newKey && (
          <div className="note note-ok" style={{ marginTop: 10 }}>
            Copy this key now; it won't be shown again: <code style={{ userSelect: "all", wordBreak: "break-all" }}>{newKey}</code>
          </div>
        )}
        <table style={{ marginTop: 10 }}>
          <thead><tr><th>Name</th><th>Key</th><th>Created</th><th>Last used</th><th /></tr></thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.key_id}>
                <td>{k.name}</td>
                <td className="mono">{k.prefix}…</td>
                <td className="muted">{k.created_at.slice(0, 16).replace("T", " ")}</td>
                <td className="muted">{k.last_used_at?.slice(0, 16).replace("T", " ") ?? "never"}</td>
                <td className="num"><button className="secondary" onClick={() => api(`/api/v1/api-keys/${k.key_id}`, { method: "DELETE" }).then(load)}>Revoke</button></td>
              </tr>
            ))}
            {keys.length === 0 && <tr><td colSpan={5} className="muted">No keys.</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  );
}
