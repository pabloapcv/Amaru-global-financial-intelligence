"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { ErrorNote } from "@/components/ui";

function Reset() {
  const token = useSearchParams().get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) return setError(new Error("Passwords do not match."));
    try {
      await api("/api/v1/auth/password-reset/confirm", { method: "POST", body: JSON.stringify({ token, password }) });
      setDone(true);
    } catch (err) { setError(err); }
  };

  return (
    <div className="login-page" style={{ maxWidth: 400, margin: "8vh auto" }}>
      <div className="brand" style={{ padding: "0 0 12px" }}><span className="brand-mark">◆</span> ATLAS</div>
      <div className="card">
        <h3 style={{ marginBottom: 12 }}>Choose a new password</h3>
        {done ? (
          <div className="note note-ok">Password updated. You've been signed out everywhere. <a href="/login">Sign in</a></div>
        ) : !token ? (
          <div className="note note-error">This reset link is missing its token. Request a new one from the sign-in page.</div>
        ) : (
          <form onSubmit={submit}>
            <div className="field"><label htmlFor="np">New password</label><input id="np" type="password" minLength={10} autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></div>
            <div className="field"><label htmlFor="cp">Repeat password</label><input id="cp" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required /></div>
            <button type="submit" style={{ marginTop: 14, width: "100%" }}>Update password</button>
          </form>
        )}
        <div style={{ marginTop: 10 }}><ErrorNote error={error} /></div>
      </div>
    </div>
  );
}

export default function ResetPage() {
  return <Suspense><Reset /></Suspense>;
}
