"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorNote } from "@/components/ui";

type Mine = { rating: "up" | "down"; reasons: string[]; comment: string; status: string; operator_note: string | null };

export default function Feedback({ runId }: { runId: string }) {
  const [mine, setMine] = useState<Mine | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState<{ reasons: string[]; comment: string } | null>(null);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    setMine(null); setDraft(null);
    api<{ feedback: Mine | null; reasons: Record<string, string> }>(`/api/v1/research/${runId}/feedback`)
      .then((r) => { setMine(r.feedback); setReasons(r.reasons); }).catch(() => {});
  }, [runId]);

  const send = async (rating: "up" | "down", body?: { reasons: string[]; comment: string }) => {
    setError(null);
    try {
      setMine(await api<Mine>(`/api/v1/research/${runId}/feedback`, { method: "PUT", body: JSON.stringify({ rating, ...(body ?? { reasons: [], comment: "" }) }) }));
      setDraft(null);
    } catch (e) { setError(e); }
  };
  const toggle = (k: string) => draft && setDraft({ ...draft, reasons: draft.reasons.includes(k) ? draft.reasons.filter((x) => x !== k) : [...draft.reasons, k] });

  return (
    <div style={{ borderTop: "1px solid var(--line)", marginTop: 16, paddingTop: 12 }}>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <span className="muted" style={{ fontSize: 13 }}>Was this answer right?</span>
        <button className={mine?.rating === "up" ? "" : "secondary"} onClick={() => send("up")} aria-pressed={mine?.rating === "up"}>Yes</button>
        <button className={mine?.rating === "down" ? "" : "secondary"} aria-pressed={mine?.rating === "down"}
          onClick={() => setDraft({ reasons: mine?.rating === "down" ? mine.reasons : [], comment: mine?.rating === "down" ? mine.comment : "" })}>Something&apos;s wrong</button>
        {mine && !draft && <span className="muted" style={{ fontSize: 12 }}>Thanks, your feedback was sent{mine.rating === "down" && mine.status === "resolved" ? " and has been reviewed" : ""}.</span>}
      </div>
      {mine?.operator_note && <div className="note" style={{ marginTop: 8, fontSize: 12.5 }}>Reply from the Amaru team: {mine.operator_note}</div>}
      {draft && (
        <div style={{ marginTop: 10 }}>
          <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
            {Object.entries(reasons).map(([k, label]) => (
              <label key={k} className="row" style={{ fontWeight: 400, margin: 0, fontSize: 12.5 }}>
                <input type="checkbox" checked={draft.reasons.includes(k)} onChange={() => toggle(k)} style={{ width: "auto" }} />{label}
              </label>
            ))}
          </div>
          <textarea rows={3} value={draft.comment} onChange={(e) => setDraft({ ...draft, comment: e.target.value })} maxLength={2000}
            placeholder="What was wrong? For example, which number, and what you expected." aria-label="Feedback comment" style={{ marginTop: 8 }} />
          <p className="muted" style={{ fontSize: 11.5 }}>Sent to the people who run Amaru with your question and the answer&apos;s title, so they can look into it.</p>
          <div className="row" style={{ gap: 8 }}>
            <button onClick={() => send("down", draft)}>Send</button>
            <button className="secondary" onClick={() => setDraft(null)}>Cancel</button>
          </div>
        </div>
      )}
      <ErrorNote error={error} />
    </div>
  );
}
