"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Member = { user_id: string; email: string; name: string; role: string; last_login_at: string | null; you: boolean };
type Invite = { invitation_id: string; email: string; role: string; expires_at: string };
type TeamData = { members: Member[]; invitations: Invite[]; role: string; seats: { used: number; limit: number | null } };

export default function Team() {
  const [team, setTeam] = useState<TeamData | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [created, setCreated] = useState<{ email: string; link: string; emailed: boolean } | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => api<TeamData>("/api/v1/team").then(setTeam).catch(setError), []);
  useEffect(() => { load(); }, [load]);
  if (!team) return null;
  const manager = team.role === "owner" || team.role === "admin";
  const full = team.seats.limit != null && team.seats.used >= team.seats.limit;

  const call = (p: Promise<unknown>) => p.then(() => { setError(null); load(); }).catch(setError);
  const invite = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setCopied(false);
    try {
      const r = await api<{ email: string; link: string; emailed: boolean }>("/api/v1/team/invitations", { method: "POST", body: JSON.stringify({ email, role }) });
      setCreated(r);
      setEmail("");
      load();
    } catch (err) { setError(err); }
  };

  return (
    <Card title={`Team · ${team.seats.used}${team.seats.limit != null ? ` of ${team.seats.limit}` : ""} seats`}>
      <table>
        <tbody>
          {team.members.map((m) => (
            <tr key={m.user_id}>
              <td>{m.name || m.email}{m.you && <span className="muted"> (you)</span>}<div className="muted" style={{ fontSize: 11.5 }}>{m.email}</div></td>
              <td>
                {team.role === "owner" && m.role !== "owner"
                  ? <select value={m.role} onChange={(e) => call(api(`/api/v1/team/members/${m.user_id}`, { method: "PATCH", body: JSON.stringify({ role: e.target.value }) }))} aria-label={`Role for ${m.email}`} style={{ width: "auto" }}>
                      <option value="member">member</option><option value="admin">admin</option>
                    </select>
                  : m.role}
              </td>
              <td className="num">
                {m.role !== "owner" && (m.you || (manager && (team.role === "owner" || m.role === "member"))) && (
                  <button className="secondary" onClick={() => { if (window.confirm(m.you ? "Leave this workspace?" : `Remove ${m.email}? Their sessions and API keys for this workspace stop working.`)) call(api(`/api/v1/team/members/${m.user_id}`, { method: "DELETE" })).then(() => m.you && (window.location.href = "/")); }}>
                    {m.you ? "Leave" : "Remove"}
                  </button>
                )}
              </td>
            </tr>
          ))}
          {team.invitations.map((i) => (
            <tr key={i.invitation_id}>
              <td>{i.email}<div className="muted" style={{ fontSize: 11.5 }}>invited · expires {i.expires_at.slice(0, 10)}</div></td>
              <td className="muted">{i.role}</td>
              <td className="num"><button className="secondary" onClick={() => call(api(`/api/v1/team/invitations/${i.invitation_id}`, { method: "DELETE" }))}>Revoke</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {manager && (
        <form className="row" onSubmit={invite} style={{ marginTop: 12, flexWrap: "wrap" }}>
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="colleague@company.com" style={{ flex: "1 1 200px", minWidth: 0 }} aria-label="Email to invite" />
          {team.role === "owner" && <select value={role} onChange={(e) => setRole(e.target.value)} style={{ width: "auto" }} aria-label="Role"><option value="member">member</option><option value="admin">admin</option></select>}
          <button type="submit" disabled={full}>Invite</button>
        </form>
      )}
      {manager && full && <p className="muted" style={{ fontSize: 12 }}>All seats are in use. Revoke an invitation, remove a member or move to a larger plan.</p>}
      {created && (
        <div className="note" style={{ marginTop: 10 }}>
          {created.emailed ? `Invitation emailed to ${created.email}. ` : `Email is not configured, so send ${created.email} this link yourself. `}It works once and expires in 7 days.
          <div className="row" style={{ flexWrap: "nowrap", marginTop: 6 }}>
            <input readOnly value={created.link} onFocus={(e) => e.target.select()} style={{ minWidth: 0, flex: 1 }} aria-label="Invitation link" />
            <button className="secondary" onClick={() => navigator.clipboard.writeText(created.link).then(() => setCopied(true)).catch(() => {})}>{copied ? "Copied" : "Copy"}</button>
          </div>
        </div>
      )}
      <ErrorNote error={error} />
    </Card>
  );
}
