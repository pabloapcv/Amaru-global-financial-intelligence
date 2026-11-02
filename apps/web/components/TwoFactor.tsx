"use client";

import { useState } from "react";
import { api, type Account } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

export default function TwoFactor({ account, onChange }: { account: Account; onChange: () => void }) {
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const on = Boolean(account.two_factor);

  const begin = async () => {
    setError(null);
    try { setSetup(await api("/api/v1/account/2fa/setup", { method: "POST" })); } catch (e) { setError(e); }
  };
  const enable = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const r = await api<{ recovery_codes: string[] }>("/api/v1/account/2fa/enable", { method: "POST", body: JSON.stringify({ code }) });
      setCodes(r.recovery_codes);
      setSetup(null);
      setCode("");
      onChange();
    } catch (err) { setError(err); }
  };
  const disable = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await api("/api/v1/account/2fa/disable", { method: "POST", body: JSON.stringify({ password, code }) });
      setPassword("");
      setCode("");
      onChange();
    } catch (err) { setError(err); }
  };

  return (
    <Card title={`Two-factor authentication · ${on ? "on" : "off"}`}>
      {codes && (
        <div className="note note-warn" style={{ marginBottom: 10 }}>
          Save these recovery codes somewhere safe. Each works once if you lose your device; they will not be shown again.
          <pre style={{ margin: "8px 0" }}>{codes.join("\n")}</pre>
          <div className="row">
            <button className="secondary" onClick={() => navigator.clipboard.writeText(codes.join("\n")).then(() => setCopied(true)).catch(() => {})}>{copied ? "Copied" : "Copy"}</button>
            <button className="secondary" onClick={() => setCodes(null)}>I have saved them</button>
          </div>
        </div>
      )}
      {!on && !setup && (
        <>
          <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>Require a code from an authenticator app (1Password, Google Authenticator, Authy…) when you sign in. Turning it on signs out your other sessions.</p>
          <button onClick={begin}>Turn on</button>
        </>
      )}
      {!on && setup && (
        <form onSubmit={enable}>
          <p style={{ marginTop: 0, fontSize: 13 }}>Add Amaru to your authenticator app with this key (or <a href={setup.otpauth_uri}>open it in the app</a> on this device), then enter the 6-digit code it shows.</p>
          <pre style={{ fontSize: 15, letterSpacing: 1, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{setup.secret.match(/.{1,4}/g)?.join(" ")}</pre>
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" autoComplete="one-time-code" placeholder="123456" maxLength={8} aria-label="Code from your authenticator app" style={{ maxWidth: 140 }} />
            <button type="submit">Confirm</button>
            <button type="button" className="secondary" onClick={() => setSetup(null)}>Cancel</button>
          </div>
        </form>
      )}
      {on && (
        <form onSubmit={disable}>
          <p className="muted" style={{ marginTop: 0, fontSize: 13 }}>
            Sign-in requires a code from your authenticator app. Recovery codes left: {account.recovery_codes_left ?? 0}.
            {(account.recovery_codes_left ?? 0) <= 2 && " Running low: turn two-factor off and on again to get new codes."}
          </p>
          <div className="row" style={{ flexWrap: "wrap" }}>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password" autoComplete="current-password" aria-label="Password" style={{ flex: "1 1 160px" }} />
            <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="Code" autoComplete="one-time-code" aria-label="Authenticator or recovery code" style={{ flex: "0 1 120px" }} />
            <button type="submit" className="secondary">Turn off</button>
          </div>
        </form>
      )}
      <ErrorNote error={error} />
    </Card>
  );
}
