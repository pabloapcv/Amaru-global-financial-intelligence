"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Statement = { statement_id: string; bank: string; meeting_date: string; title: string; url: string; read: boolean;
  action?: string; change_bp?: number; stance?: number; stance_label?: string; compared_with?: string | null; balance_sheet?: string;
  dissents?: number | null; summary?: string; text_change_bp?: number | null; consistent?: boolean };
type Overview = { banks: { bank: string; name: string; statements: Statement[] }[]; unread: number; caveat: string };

const decision = (s: Statement) => s.action === "hold" ? "Held" : s.action === "other" ? "No rate decision" : `${s.action === "hike" ? "Raised" : "Cut"} ${Math.abs(s.change_bp ?? 0)} bp`;

function Trend({ rows }: { rows: Statement[] }) {
  const read = rows.filter((r) => r.read && r.compared_with).reverse();
  if (read.length < 2) return null;
  return (
    <div className="row" style={{ gap: 3, alignItems: "center", height: 44, marginBottom: 8 }} aria-label="Tone at recent meetings, oldest first">
      {read.map((r) => {
        const v = r.stance ?? 0;
        return (
          <div key={r.statement_id} title={`${r.meeting_date}: ${r.stance_label} (${v > 0 ? "+" : ""}${v})`} style={{ position: "relative", flex: "1 1 0", maxWidth: 28, height: 44 }}>
            <div style={{ position: "absolute", left: 0, right: 0, top: 22, height: 1, background: "var(--line)" }} />
            <div style={{ position: "absolute", left: 3, right: 3, borderRadius: 2, top: v > 0 ? 22 - v * 10 : 22, height: v ? Math.abs(v) * 10 : 2,
              background: v > 0 ? "var(--down)" : v < 0 ? "var(--up)" : "var(--muted)" }} />
          </div>
        );
      })}
      <span className="muted" style={{ fontSize: 11, marginLeft: 6 }}>Up is more hawkish</span>
    </div>
  );
}

export default function CentralBankTone() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => { api<Overview>("/api/v1/central-banks/tone").then(setData).catch(setError); }, []);

  const empty = data && data.banks.every((b) => b.statements.length === 0);
  return (
    <Card title="Central-bank tone">
      <ErrorNote error={error} />
      {empty && <p className="muted" style={{ margin: 0 }}>No statements yet. They are fetched with the regular data refresh, or with <code>atlas ingest --tone</code>.</p>}
      {data && !empty && (
        <>
          <div className="grid grid-2">
            {data.banks.map((b) => {
              const latest = b.statements.find((s) => s.read);
              return (
                <div key={b.bank}>
                  <div style={{ fontWeight: 600, marginBottom: 6 }}>{b.name}</div>
                  <Trend rows={b.statements} />
                  {!latest && <p className="muted" style={{ fontSize: 13 }}>Statements are stored but haven't been read yet.</p>}
                  {latest && (
                    <>
                      <div className="muted" style={{ fontSize: 11.5 }}>
                        <a href={latest.url} target="_blank" rel="noreferrer">{latest.title}, {latest.meeting_date}</a>
                      </div>
                      <div style={{ fontSize: 13.5, margin: "4px 0" }}>
                        <strong>{decision(latest)}</strong> · {latest.stance_label}{latest.compared_with ? ` (${(latest.stance ?? 0) > 0 ? "+" : ""}${latest.stance})` : ""}
                        {latest.dissents ? ` · ${latest.dissents} ${latest.dissents === 1 ? "dissent" : "dissents"}` : ""}
                      </div>
                      <p className="muted" style={{ fontSize: 13, margin: 0 }}>{latest.summary}</p>
                      {latest.consistent === false && <div className="note note-warn" style={{ marginTop: 8, fontSize: 12.5 }}>The decision read by Claude doesn't match the statement's text ({latest.text_change_bp} bp). Check the statement itself.</div>}
                    </>
                  )}
                </div>
              );
            })}
          </div>
          {data.unread > 0 && <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{data.unread} statement{data.unread === 1 ? "" : "s"} not read yet.</p>}
          <p className="muted" style={{ fontSize: 11.5, marginBottom: 0 }}>{data.caveat}</p>
        </>
      )}
    </Card>
  );
}
