"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, fmt } from "@/lib/api";
import { Card } from "@/components/ui";

type Briefing = {
  events: { date: string; end_date: string | null; kind: string; title: string; url: string | null }[];
  series_due: { name: string; next_period_due: string | null; official_release_date: string | null }[];
  calendar_window: { from: string; to: string };
  releases: { title: string; publisher: string; published: string | null; url: string }[];
  usda: { name: string; instrument_id: string | null; usda_release: string; market_year: string; stocks_to_use_pct: number | null;
    stocks_to_use_percentile: number | null; stocks_to_use_change_pp: number | null;
    largest_revision: { market_year: string; attribute: string; change_pct: number | null } | null }[];
};

const day = (iso: string) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
const small = { fontSize: 11.5 } as const;

export default function ThisWeek() {
  const [b, setB] = useState<Briefing | null>(null);
  useEffect(() => { api<Briefing>("/api/v1/briefing?days=14").then(setB).catch(() => setB(null)); }, []);
  if (!b) return null;
  const due = b.series_due.filter((d) => d.next_period_due);
  return (
    <div className="grid grid-3" style={{ marginBottom: 18 }}>
      <Card title={<Link href="/economy">Next two weeks</Link>}>
        {b.events.length === 0 && <p className="muted">No scheduled events.</p>}
        <ul className="list-tight">
          {b.events.map((e) => (
            <li key={e.date + e.title}>
              <span className="muted" style={small}>{day(e.date)}{e.end_date && e.end_date !== e.date ? ` – ${day(e.end_date)}` : ""}</span><br />
              {e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.title}</a> : e.title}
            </li>
          ))}
        </ul>
        {due.length > 0 && (
          <p className="muted" style={{ ...small, marginTop: 8 }}>
            U.S. data due: {due.map((d) => `${d.name.replace(/^U\.S\. /, "").split(" (")[0]} (${d.next_period_due}${d.official_release_date ? `, ${day(d.official_release_date)}` : ""})`).join(" · ")}
          </p>
        )}
      </Card>
      <Card title={<Link href="/intelligence">Official releases · 7 days</Link>}>
        {b.releases.length === 0 && <p className="muted">No new releases from the Fed, BEA or CFTC.</p>}
        <ul className="list-tight">
          {b.releases.map((r) => (
            <li key={r.url}>
              <span className="muted" style={small}>{r.published ?? "undated"} · {r.publisher}</span><br />
              <a href={r.url} target="_blank" rel="noreferrer" style={{ overflowWrap: "anywhere" }}>{r.title}</a>
            </li>
          ))}
        </ul>
      </Card>
      <Card title="USDA stocks-to-use">
        <table>
          <tbody>
            {b.usda.map((u) => (
              <tr key={u.name}>
                <td>
                  {u.instrument_id ? <Link href={`/markets?id=${u.instrument_id}`}>{u.name}</Link> : u.name}
                  <div className="muted" style={small}>{u.market_year} · report {u.usda_release}</div>
                </td>
                <td className="num" style={{ whiteSpace: "nowrap" }}>{fmt(u.stocks_to_use_pct, 1)}%
                  <div className="muted" style={small} title="Change versus the prior market year; percentile versus the previous 20 market years">
                    {u.stocks_to_use_change_pp != null ? `${u.stocks_to_use_change_pp > 0 ? "+" : ""}${fmt(u.stocks_to_use_change_pp, 1)} pp` : ""} · p{fmt(u.stocks_to_use_percentile, 0)}
                  </div>
                  {u.largest_revision?.change_pct != null && <div className="muted" style={small}>rev. {u.largest_revision.attribute.replace("_", " ")} {u.largest_revision.change_pct > 0 ? "+" : ""}{fmt(u.largest_revision.change_pct, 1)}%</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
