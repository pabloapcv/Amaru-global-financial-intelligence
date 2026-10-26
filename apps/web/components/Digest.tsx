"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

export default function Digest() {
  const [weekly, setWeekly] = useState<boolean | null>(null);
  const [preview, setPreview] = useState<{ subject: string; body: string } | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { api<{ weekly: boolean }>("/api/v1/account/digest").then((r) => setWeekly(r.weekly)).catch(setError); }, []);
  if (weekly === null) return null;

  const toggle = async (on: boolean) => {
    setError(null);
    try { setWeekly((await api<{ weekly: boolean }>("/api/v1/account/digest", { method: "PUT", body: JSON.stringify({ weekly: on }) })).weekly); }
    catch (e) { setError(e); }
  };
  const show = async () => {
    setError(null);
    try { setPreview(preview ? null : await api("/api/v1/account/digest/preview")); } catch (e) { setError(e); }
  };

  return (
    <Card title="Weekly email" actions={<button className="secondary" onClick={show}>{preview ? "Hide preview" : "Preview"}</button>}>
      <label className="row" style={{ fontWeight: 400, margin: 0 }}>
        <input type="checkbox" checked={weekly} onChange={(e) => toggle(e.target.checked)} style={{ width: "auto" }} />
        Send me a weekly digest for this workspace (Mondays, from 07:00 UTC)
      </label>
      <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
        Your watchlist&apos;s moves, the week&apos;s scheduled events and U.S. data, new official releases, USDA updates, and your alerts and agents. Every email has a one-click unsubscribe link.
      </p>
      {preview && (
        <div style={{ marginTop: 10 }}>
          <div className="muted" style={{ fontSize: 12 }}>Subject: {preview.subject}</div>
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12.5, maxHeight: 420, overflowY: "auto" }}>{preview.body}</pre>
        </div>
      )}
      <ErrorNote error={error} />
    </Card>
  );
}
