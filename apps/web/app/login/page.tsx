"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { ErrorNote } from "@/components/ui";

function Login() {
  const params = useSearchParams();
  const [mode, setMode] = useState<"login" | "signup" | "forgot">(params.get("mode") === "signup" ? "signup" : "login");
  const [notice, setNotice] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [signupAllowed, setSignupAllowed] = useState(true);
  const [legal, setLegal] = useState<{ terms_url: string | null; privacy_url: string | null }>({ terms_url: null, privacy_url: null });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [challenge, setChallenge] = useState<string | null>(null);
  const [code, setCode] = useState("");

  useEffect(() => {
    api<{ signup_allowed: boolean; terms_url: string | null; privacy_url: string | null }>("/health").then((h) => {
      setSignupAllowed(h.signup_allowed);
      setLegal({ terms_url: h.terms_url ?? null, privacy_url: h.privacy_url ?? null });
      if (!h.signup_allowed) setMode((m) => (m === "signup" ? "login" : m));
    }).catch(() => {});
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "forgot") {
        const r = await api<{ message: string }>("/api/v1/auth/password-reset/request", { method: "POST", body: JSON.stringify({ email }) });
        setNotice(r.message);
        return;
      }
      if (challenge) {
        await api("/api/v1/auth/login/2fa", { method: "POST", body: JSON.stringify({ challenge, code }) });
      } else if (mode === "login") {
        const r = await api<{ two_factor_required?: boolean; challenge?: string }>("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ email, password }) });
        if (r.two_factor_required && r.challenge) {
          setChallenge(r.challenge);
          setPassword("");
          return;
        }
      } else {
        await api("/api/v1/auth/signup", { method: "POST", body: JSON.stringify({ email, password, name, workspace_name: workspace || undefined }) });
      }
      const next = params.get("next");
      window.location.href = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page" style={{ maxWidth: 400, margin: "8vh auto" }}>
      <div className="brand" style={{ padding: "0 0 12px" }}><span className="brand-mark">◆</span> ATLAS</div>
      <div className="card">
        <div className="seg" style={{ marginBottom: 14 }}>
          <button type="button" className={mode === "login" ? "on" : ""} onClick={() => setMode("login")}>Sign in</button>
          {signupAllowed && <button type="button" className={mode === "signup" ? "on" : ""} onClick={() => setMode("signup")}>Create account</button>}
        </div>
        {!challenge && <p className="muted" style={{ fontSize: 12.5, marginTop: -6 }}>New to ATLAS? <a href="/welcome">See what it does and the plans</a>.</p>}
        {challenge ? (
          <form onSubmit={submit}>
            <p style={{ marginTop: 0 }}>Enter the 6-digit code from your authenticator app, or one of your recovery codes.</p>
            <div className="field"><label htmlFor="otp">Code</label>
              <input id="otp" value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" autoFocus required maxLength={20} />
            </div>
            <button type="submit" disabled={busy} style={{ marginTop: 14, width: "100%" }}>{busy ? "…" : "Verify"}</button>
            <p style={{ fontSize: 12.5, marginBottom: 0 }}><a href="#" onClick={(e) => { e.preventDefault(); setChallenge(null); setCode(""); setError(null); }}>Start over</a></p>
          </form>
        ) : (
        <form onSubmit={submit}>
          {mode === "signup" && (
            <>
              <div className="field"><label htmlFor="name">Name</label><input id="name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></div>
              <div className="field"><label htmlFor="ws">Workspace name</label><input id="ws" value={workspace} onChange={(e) => setWorkspace(e.target.value)} placeholder="e.g. Commodities desk" /></div>
            </>
          )}
          <div className="field"><label htmlFor="email">Email</label><input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required /></div>
          {mode !== "forgot" && <div className="field">
            <label htmlFor="pw">Password</label>
            <input id="pw" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={mode === "signup" ? 10 : undefined} required />
            {mode === "signup" && <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>At least 10 characters.</p>}
          </div>}
          <button type="submit" disabled={busy} style={{ marginTop: 14, width: "100%" }}>{busy ? "…" : mode === "login" ? "Sign in" : mode === "signup" ? "Create account" : "Send reset link"}</button>
          {mode === "signup" && (legal.terms_url || legal.privacy_url) && (
            <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
              By creating an account you agree to the {legal.terms_url && <a href={legal.terms_url} target="_blank" rel="noreferrer">Terms of Service</a>}
              {legal.terms_url && legal.privacy_url && " and the "}
              {legal.privacy_url && <a href={legal.privacy_url} target="_blank" rel="noreferrer">Privacy Policy</a>}.
            </p>
          )}
        </form>
        )}
        {mode === "login" && !challenge && <p style={{ fontSize: 12.5, marginBottom: 0 }}><a href="#" onClick={(e) => { e.preventDefault(); setMode("forgot"); setNotice(null); }}>Forgot password?</a></p>}
        {mode === "forgot" && <p style={{ fontSize: 12.5, marginBottom: 0 }}><a href="#" onClick={(e) => { e.preventDefault(); setMode("login"); setNotice(null); }}>Back to sign in</a></p>}
        {notice && <div className="note note-ok" style={{ marginTop: 10 }}>{notice}</div>}
        <div style={{ marginTop: 10 }}><ErrorNote error={error} /></div>
      </div>
      <p className="muted" style={{ fontSize: 12, textAlign: "center" }}>Research and decision support only. Not investment advice.</p>
    </div>
  );
}

export default function LoginPage() {
  return <Suspense><Login /></Suspense>;
}
