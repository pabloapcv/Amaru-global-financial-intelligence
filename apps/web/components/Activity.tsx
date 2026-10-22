"use client";

import { useCallback, useEffect, useState } from "react";
import { API_URL, api } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Event = { event_id: string; at: string; action: string; label: string; actor: string | null; detail: Record<string, unknown> };
type Page = { events: Event[]; next_cursor: string | null };

const CATEGORIES: [string, string][] = [["", "All"], ["signin", "Sign-ins"], ["team", "Team"], ["sharing", "Sharing"], ["data", "Exports"], ["keys", "API keys"], ["security", "Security"]];

function summary(d: Record<string, unknown>) {
  const parts = Object.entries(d).filter(([k]) => !k.endsWith("_id") || k === "dataset_id").map(([k, v]) => `${k.replace("_", " ")}: ${String(v)}`);
  return parts.join(" · ");
}

export default function Activity() {
  const [category, setCategory] = useState("");
  const [events, setEvents] = useState<Event[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (cursor: string | null) => {
    const q = new URLSearchParams({ limit: "50", ...(category ? { category } : {}), ...(cursor ? { cursor } : {}) });
    try {
      const page = await api<Page>(`/api/v1/activity?${q}`);
      setEvents((prev) => (cursor ? [...prev, ...page.events] : page.events));
      setNext(page.next_cursor);
    } catch (e) { setError(e); }
  }, [category]);
  useEffect(() => { load(null); }, [load]);

  return (
    <Card
      title="Activity"
      actions={<a href={`${API_URL}/api/v1/activity?format=csv&limit=500${category ? `&category=${category}` : ""}`}>CSV</a>}
    >
      <div className="seg" style={{ marginBottom: 10 }}>
        {CATEGORIES.map(([k, l]) => <button key={k} className={k === category ? "on" : ""} onClick={() => setCategory(k)}>{l}</button>)}
      </div>
      <ErrorNote error={error} />
      <table>
        <tbody>
          {events.map((e) => (
            <tr key={e.event_id}>
              <td className="muted" style={{ whiteSpace: "nowrap", fontSize: 12 }}>{e.at.replace("T", " ").slice(0, 16)}</td>
              <td>{e.label}<div className="muted" style={{ fontSize: 11.5, overflowWrap: "anywhere" }}>{summary(e.detail)}</div></td>
              <td className="muted" style={{ fontSize: 12 }}>{e.actor ?? (e.action === "report.share_viewed" ? "link visitor" : "—")}</td>
            </tr>
          ))}
          {events.length === 0 && <tr><td className="muted">No activity in this category yet.</td></tr>}
        </tbody>
      </table>
      {next && <button className="secondary" style={{ marginTop: 10 }} onClick={() => load(next)}>Load more</button>}
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>Sign-ins, team changes, share links and their views, data exports and API keys in this workspace. Visible to owners and admins.</p>
    </Card>
  );
}
