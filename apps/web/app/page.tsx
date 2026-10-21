"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, fmt, type Instrument, type Quote } from "@/lib/api";
import { Card, Change, ErrorNote, FreshnessBadge, PageHeader } from "@/components/ui";
import GetStarted from "@/components/GetStarted";
import ThisWeek from "@/components/ThisWeek";

const GROUPS: { title: string; ids: string[] }[] = [
  { title: "Commodities", ids: ["CMD.SUGAR.ISA", "CMD.COPPER.LME", "CMD.CRUDE.BRENT", "CMD.CRUDE.WTI", "CMD.NATGAS.HH", "CMD.COFFEE.ARABICA", "CMD.WHEAT.USHRW", "CMD.GOLD"] },
  { title: "Currencies", ids: ["FX.USD.BROAD", "FX.USDBRL", "FX.USDMXN", "FX.USDINR", "FX.USDCNY", "FX.EURUSD"] },
  { title: "Rates & risk", ids: ["RATE.US.FEDFUNDS", "RATE.UST.2Y", "RATE.UST.10Y", "RATE.UST.10Y.REAL", "EQ.US.SP500", "VOL.US.VIX"] },
];

function QuoteCard({ q }: { q: Quote }) {
  const isRate = q.unit === "percent";
  return (
    <Link href={`/markets?id=${q.dataset_id}`} className="card quote" style={{ color: "inherit", textDecoration: "none" }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start", flexWrap: "nowrap" }}>
        <span className="name">{q.name}</span>
        <FreshnessBadge value={q.freshness} title={q.freshness_note} />
      </div>
      {q.error ? (
        <div className="muted" style={{ marginTop: 8, fontSize: 12.5 }}>{q.freshness_note}</div>
      ) : (
        <>
          <div className="value">{fmt(q.value, isRate ? 2 : q.value! > 100 ? 2 : 4)}{isRate && "%"}</div>
          <div className="sub">
            <span>{isRate ? <Change value={q.change !== undefined ? q.change * 100 : null} unit="bp" /> : <Change value={q.change_pct} />}</span>
            <span>{q.observation_date}</span>
          </div>
        </>
      )}
    </Link>
  );
}

export default function Overview() {
  const [quotes, setQuotes] = useState<Record<string, Quote>>({});
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [health, setHealth] = useState<{ llm_configured: boolean; llm_model: string } | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const ids = GROUPS.flatMap((g) => g.ids).join(",");
    Promise.all([
      api<Quote[]>(`/api/v1/markets/quotes?ids=${ids}`),
      api<Instrument[]>("/api/v1/instruments"),
      api<{ llm_configured: boolean; llm_model: string }>("/health"),
    ])
      .then(([qs, inst, h]) => {
        setQuotes(Object.fromEntries(qs.map((q) => [q.dataset_id, q])));
        setInstruments(inst);
        setHealth(h);
      })
      .catch(setError);
  }, []);

  const counts = instruments.reduce<Record<string, number>>((acc, i) => ((acc[i.freshness] = (acc[i.freshness] ?? 0) + 1), acc), {});

  return (
    <>
      <PageHeader
        title="Global overview"
        subtitle="Latest stored observations from connected public sources. Reference and benchmark prices, published with a lag — not live tradable quotes."
        actions={<Link href="/research"><button>Ask ATLAS</button></Link>}
      />
      <GetStarted />
      <ErrorNote error={error} />
      {!error && instruments.length === 0 && <p className="muted">Loading latest observations…</p>}
      <div className="grid grid-4" style={{ marginBottom: 18 }}>
        <Card title="Datasets">
          <div className="quote"><div className="value">{instruments.length || "—"}</div></div>
          <div className="row">{Object.entries(counts).map(([k, v]) => <FreshnessBadge key={k} value={k as any} title={`${v}`} />)}</div>
        </Card>
        <Card title="Current">
          <div className="quote"><div className="value up">{counts.current ?? 0}</div><div className="sub">within expected publication lag</div></div>
        </Card>
        <Card title="Gaps">
          <div className="quote"><div className="value">{(counts.no_source ?? 0) + (counts.failing ?? 0)}</div><div className="sub">no source or failing</div></div>
        </Card>
        <Card title="Research engine">
          <div className="quote"><div className="value" style={{ fontSize: 16 }}>{health ? (health.llm_configured ? health.llm_model : "Deterministic") : "—"}</div>
          <div className="sub">{health ? (health.llm_configured ? "Claude orchestration enabled" : "Set ANTHROPIC_API_KEY to enable Claude") : ""}</div></div>
        </Card>
      </div>
      <ThisWeek />
      {GROUPS.map((g) => (
        <div key={g.title} style={{ marginBottom: 18 }}>
          <h3 style={{ marginBottom: 10 }}>{g.title}</h3>
          <div className="grid grid-4">
            {g.ids.map((id) => quotes[id] && <QuoteCard key={id} q={quotes[id]} />)}
          </div>
        </div>
      ))}
    </>
  );
}
