"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import Chart from "@/components/Chart";
import { API_URL, api, type ResearchRun } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

const EXAMPLES = [
  "What's happening with sugar futures today, and what are the main drivers? Outlook for the next three months.",
  "Analyze the relationship between the Brazilian real and sugar prices over the last five years.",
  "What happens to copper, gold, and emerging-market currencies if the Federal Reserve cuts interest rates?",
  "How is the US economy performing right now?",
  "Identify unusual price movements across commodities.",
  "Copper outlook: China demand proxies, dollar and equities, next six months.",
];

type RunSummary = { run_id: string; query: string; status: string; title: string | null; created_at: string };

function linkCitations(md: string) {
  return md.replace(/\[(E\d+(?:\s*,\s*E\d+)*)\]/g, (_, labels: string) =>
    labels.split(/\s*,\s*/).map((l) => `[${l}](#ev-${l})`).join(" "),
  );
}

function ResearchForm({ onSubmitted }: { onSubmitted: (id: string) => void }) {
  const [query, setQuery] = useState(EXAMPLES[0]);
  const [depth, setDepth] = useState<"quick" | "standard" | "deep">("standard");
  const [horizon, setHorizon] = useState("3M");
  const [lookback, setLookback] = useState("5Y");
  const [engine, setEngine] = useState<"auto" | "llm" | "deterministic">("auto");
  const [xlsx, setXlsx] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ run_id: string }>("/api/v1/research", {
        method: "POST",
        body: JSON.stringify({
          query, depth, engine,
          scope: { lookback, forecast_horizon: horizon },
          report: { formats: xlsx ? ["html", "xlsx"] : ["html"] },
        }),
      });
      onSubmitted(r.run_id);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="New research">
      <div className="field">
        <label htmlFor="q">Research question</label>
        <textarea id="q" value={query} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <div className="field">
        <label>Depth</label>
        <div className="seg">
          {(["quick", "standard", "deep"] as const).map((d) => (
            <button key={d} className={d === depth ? "on" : ""} onClick={() => setDepth(d)}>{d[0].toUpperCase() + d.slice(1)}</button>
          ))}
        </div>
      </div>
      <div className="grid grid-2" style={{ marginTop: 12 }}>
        <div><label>Lookback</label><select value={lookback} onChange={(e) => setLookback(e.target.value)}>{["1Y", "3Y", "5Y", "10Y", "20Y"].map((v) => <option key={v}>{v}</option>)}</select></div>
        <div><label>Outlook horizon</label><select value={horizon} onChange={(e) => setHorizon(e.target.value)}>{["1M", "3M", "6M", "12M"].map((v) => <option key={v}>{v}</option>)}</select></div>
      </div>
      <div className="field" style={{ marginTop: 12 }}>
        <label>Engine</label>
        <select value={engine} onChange={(e) => setEngine(e.target.value as any)}>
          <option value="auto">Auto (Claude if configured)</option>
          <option value="llm">Claude (LLM orchestration)</option>
          <option value="deterministic">Deterministic (no LLM)</option>
        </select>
      </div>
      <div className="row" style={{ marginTop: 12, justifyContent: "space-between" }}>
        <label className="row" style={{ margin: 0, fontWeight: 400 }}>
          <input type="checkbox" checked={xlsx} onChange={(e) => setXlsx(e.target.checked)} style={{ width: "auto" }} /> Excel export
        </label>
        <button onClick={submit} disabled={busy || query.trim().length < 3}>{busy ? "Submitting…" : "Run research"}</button>
      </div>
      <ErrorNote error={error} />
      <details style={{ marginTop: 12 }}>
        <summary className="muted" style={{ cursor: "pointer" }}>Example questions</summary>
        <ul className="list-tight">
          {EXAMPLES.map((e) => <li key={e}><a href="#" onClick={(ev) => { ev.preventDefault(); setQuery(e); }}>{e}</a></li>)}
        </ul>
      </details>
    </Card>
  );
}

function RunView({ runId, onDone }: { runId: string; onDone?: () => void }) {
  const [run, setRun] = useState<ResearchRun | null>(null);
  const [error, setError] = useState<unknown>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const poll = useCallback(async () => {
    try {
      const r = await api<ResearchRun>(`/api/v1/research/${runId}`);
      setRun(r);
      if (r.status === "queued" || r.status === "running") timer.current = setTimeout(poll, 1200);
      else onDone?.();
    } catch (e) {
      setError(e);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    setRun(null);
    poll();
    return () => clearTimeout(timer.current);
  }, [poll]);

  if (error) return <ErrorNote error={error} />;
  if (!run) return <Card><span className="muted">Loading…</span></Card>;
  const res = run.result;
  const active = run.status === "queued" || run.status === "running";

  return (
    <div className="stack">
      {(active || !res) && (
        <Card title={`Research ${run.status}`} actions={active && <button className="secondary" onClick={() => api(`/api/v1/research/${runId}/cancel`, { method: "POST" })}>Cancel</button>}>
          <p style={{ marginTop: 0 }}><em>{run.query}</em></p>
          <ul className="progress">{run.progress.map((p, i) => <li key={i}>{p.at.slice(11, 19)} · {p.message}</li>)}</ul>
          {run.error && <div className="note note-error">{run.error}</div>}
        </Card>
      )}
      {res && (
        <>
          <Card
            title={`${res.engine === "llm" ? res.model : "Deterministic engine"} · ${res.evidence.length} evidence items`}
            actions={res.report_id && (
              <div className="row">
                <a href={`${API_URL}/api/v1/reports/${res.report_id}/download?format=html&inline=true`} target="_blank"><button className="secondary">Open report</button></a>
                <a href={`${API_URL}/api/v1/reports/${res.report_id}/download?format=xlsx`}><button className="secondary">Excel</button></a>
              </div>
            )}
          >
            {res.verification.passed ? (
              <div className="note note-ok">Verified: {res.verification.numbers_verified}/{res.verification.numbers_checked} numerical claims trace to stored observations or calculations; all citations resolve.</div>
            ) : (
              <div className="note note-warn">
                {res.verification.unverified_numbers.length} figure(s) could not be traced to evidence
                {res.verification.invalid_citations.length > 0 && `; invalid citations: ${res.verification.invalid_citations.join(", ")}`}.
                <ul>{res.verification.unverified_numbers.slice(0, 8).map((u, i) => <li key={i}><code>{u.value}</code> — …{u.context}…</li>)}</ul>
              </div>
            )}
            <div className="markdown">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  a: ({ href, children }) =>
                    href?.startsWith("#ev-") ? <a href={href} className="cite">{children}</a> : <a href={href} target="_blank">{children}</a>,
                }}
              >
                {linkCitations(res.narrative_markdown)}
              </ReactMarkdown>
            </div>
          </Card>
          {res.charts.length > 0 && (
            <div className="stack">
              {res.charts.map((c, i) => (
                <Card key={i} title={<>{c.title} <span className="cite">{c.evidence}</span></>}>
                  <Chart spec={c} height={260} />
                </Card>
              ))}
            </div>
          )}
          <Card title="Evidence">
            <table>
              <thead><tr><th>ID</th><th>Tool</th><th>Citation</th></tr></thead>
              <tbody>
                {res.evidence.map((e) => (
                  <tr key={e.label} id={`ev-${e.label}`}>
                    <td><span className="cite">{e.label}</span></td>
                    <td className="mono">{e.tool}<div className="muted args">{JSON.stringify(e.args)}</div></td>
                    <td style={{ fontSize: 12.5 }}>{e.citation}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          {run.plan && (
            <Card title="Research plan">
              <pre>{JSON.stringify({ intent: run.plan.intent, entities: run.plan.entities, timeframe: run.plan.timeframe, steps: run.plan.steps }, null, 2)}</pre>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

function Research() {
  const params = useSearchParams();
  const router = useRouter();
  const runId = params.get("run");
  const [runs, setRuns] = useState<RunSummary[]>([]);

  const refresh = useCallback(() => api<RunSummary[]>("/api/v1/research?limit=20").then(setRuns).catch(() => {}), []);
  useEffect(() => { refresh(); }, [refresh, runId]);

  return (
    <>
      <PageHeader title="AI Research" subtitle="Ask a financial or economic question. Every figure is computed by deterministic tools and cited to stored evidence." />
      <div className="split">
        <div className="stack">
          <ResearchForm onSubmitted={(id) => router.push(`/research?run=${id}`)} />
          <Card title="Recent">
            <ul className="list-tight">
              {runs.map((r) => (
                <li key={r.run_id}>
                  <a href={`/research?run=${r.run_id}`} onClick={(e) => { e.preventDefault(); router.push(`/research?run=${r.run_id}`); }}>{r.title ?? r.query}</a>
                  <div className="muted" style={{ fontSize: 11.5 }}>{r.status} · {r.created_at.replace("T", " ").slice(0, 16)}</div>
                </li>
              ))}
              {runs.length === 0 && <li className="muted">No research yet.</li>}
            </ul>
          </Card>
        </div>
        <div>{runId ? <RunView runId={runId} onDone={refresh} /> : <Card><p className="muted" style={{ margin: 0 }}>Results appear here. ATLAS resolves instruments, plans the investigation, runs calculations, verifies every number against evidence, and renders a versioned report.</p></Card>}</div>
      </div>
    </>
  );
}

export default function ResearchPage() {
  return <Suspense><Research /></Suspense>;
}
