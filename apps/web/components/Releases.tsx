"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Doc = { title: string; publisher: string; source: string; category: string | null; published: string | null; url: string; summary: string | null };
type Result = { results: Doc[]; indexed_sources: string[]; coverage: string };

const SOURCES: Record<string, string> = { fed_press: "Federal Reserve", bea: "BEA", cftc_press: "CFTC" };

export default function Releases() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<string | null>(null);
  const [days, setDays] = useState(30);
  const [data, setData] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    setError(null);
    api<Result>(`/api/v1/documents?q=${encodeURIComponent(query)}&days=${days}&limit=60`).then(setData).catch(setError);
  }, [query, days]);

  const rows = (data?.results ?? []).filter((d) => !source || d.source === source);

  return (
    <Card
      title="Official releases"
      actions={<div className="seg">{[7, 30, 90].map((d) => <button key={d} className={d === days ? "on" : ""} onClick={() => setDays(d)}>{d}d</button>)}</div>}
    >
      <form className="row" style={{ marginBottom: 12, flexWrap: "wrap" }} onSubmit={(e) => { e.preventDefault(); setQuery(q.trim()); }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search, e.g. GDP, discount rate, swap" style={{ flex: "1 1 220px" }} />
        <button type="submit">Search</button>
        <div className="seg">
          <button type="button" className={!source ? "on" : ""} onClick={() => setSource(null)}>All</button>
          {Object.entries(SOURCES).map(([k, v]) => <button type="button" key={k} className={source === k ? "on" : ""} onClick={() => setSource(k)}>{v}</button>)}
        </div>
      </form>
      <ErrorNote error={error} />
      {data && rows.length === 0 && <p className="muted">No releases{query ? ` match “${query}”` : ""} in the last {days} days.</p>}
      <div className="stack" style={{ gap: 0 }}>
        {rows.map((d) => (
          <div key={d.url} style={{ padding: "10px 0", borderTop: "1px solid var(--line)" }}>
            <div className="muted" style={{ fontSize: 11.5 }}>{d.published ?? "undated"} · {d.publisher}{d.category ? ` · ${d.category}` : ""}</div>
            <a href={d.url} target="_blank" rel="noreferrer" style={{ overflowWrap: "anywhere" }}>{d.title}</a>
            {d.summary && <p className="muted" style={{ fontSize: 12.5, margin: "4px 0 0" }}>{d.summary}</p>}
          </div>
        ))}
      </div>
      {data && <p className="muted" style={{ fontSize: 11.5, marginTop: 10 }}>{data.coverage}</p>}
    </Card>
  );
}
