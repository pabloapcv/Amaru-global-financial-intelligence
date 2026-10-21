"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, type Account } from "@/lib/api";
import { ErrorNote } from "@/components/ui";

type Invite = { workspace_name: string; email: string; role: string; invited_by: string | null; expires_at: string; has_account: boolean };

function Join() {
  const token = useSearchParams().get("token") ?? "";
  const [invite, setInvite] = useState<Invite | null>(null);
  const [me, setMe] = useState<Account | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!token) { setFailed("This link is missing its token."); return; }
    api<Invite>(`/api/v1/auth/invitations/${encodeURIComponent(token)}`).then(setInvite).catch(() => setFailed("This invitation is invalid, already used, or expired. Ask for a new one."));
    api<Account>("/api/v1/account").then(setMe).catch(() => setMe(null));
  }, [token]);

  const accept = async (e?: React.FormEvent) => {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/v1/auth/invitations/accept", { method: "POST", body: JSON.stringify(me ? { token } : { token, password, name }) });
      window.location.href = "/";
    } catch (err) { setError(err); } finally { setBusy(false); }
  };
  const next = `/login?next=${encodeURIComponent(`/join?token=${token}`)}`;

  return (
    <div className="login-page" style={{ maxWidth: 420, margin: "8vh auto" }}>
      <div className="brand" style={{ padding: "0 0 12px" }}><span className="brand-mark">◆</span> ATLAS</div>
      <div className="card">
        <h3 style={{ marginBottom: 12 }}>Join a workspace</h3>
        {failed && <div className="note note-error">{failed}</div>}
        {!failed && !invite && <p className="muted">Checking your invitation…</p>}
        {invite && (
          <>
            <p style={{ marginTop: 0 }}>
              {invite.invited_by ?? "A teammate"} invited <strong>{invite.email}</strong> to <strong>{invite.workspace_name}</strong> as {invite.role}.
            </p>
            {me && me.user.email === invite.email && <button onClick={() => accept()} disabled={busy}>{busy ? "Joining…" : `Join ${invite.workspace_name}`}</button>}
            {me && me.user.email !== invite.email && (
              <div className="note note-warn">You are signed in as {me.user.email}. This invitation is for {invite.email}: sign out, then open this link again.</div>
            )}
            {!me && invite.has_account && <p>You already have an account. <a href={next}>Sign in as {invite.email}</a> to accept.</p>}
            {!me && !invite.has_account && (
              <form onSubmit={accept}>
                <div className="field"><label htmlFor="jn">Your name</label><input id="jn" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></div>
                <div className="field"><label htmlFor="jp">Choose a password (10+ characters)</label><input id="jp" type="password" minLength={10} required value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" /></div>
                <button type="submit" disabled={busy}>{busy ? "Creating account…" : "Create account and join"}</button>
              </form>
            )}
            <ErrorNote error={error} />
          </>
        )}
      </div>
    </div>
  );
}

export default function JoinPage() {
  return <Suspense><Join /></Suspense>;
}
