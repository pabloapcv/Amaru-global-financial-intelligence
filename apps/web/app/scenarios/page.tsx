"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Instrument, type ResearchRun } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

const QUERY = "Scenario outlook: which scenarios does the current data point to, and what would each mean across markets?";
const DEFAULT_UNIVERSE = ["CMD.CRUDE.BRENT", "CMD.COPPER.LME", "CMD.SUGAR.ISA", "CMD.CORN.US", "CMD.SOYBEANS.US", "CMD.COFFEE.ARABICA",
  "FX.USD.BROAD", "FX.USDBRL", "FX.USDMXN", "RATE.UST.10Y", "EQ.US.SP500"];

type Past = { n: number; too_few: boolean; median?: number; low?: number; high?: number; same_direction?: number; disagrees?: boolean };
type Tested = { n: number; too_few: boolean; right_way?: number; error_ratio?: number | null };
type Impact = { dataset_id: string; name: string; implied_move: number; unit: "pct" | "bp"; ci95: [number, number] | null; significant: boolean; is_driver: boolean; past?: Past | null; tested?: Tested | null };
type TestRecord = { episodes: number; markets: number; moves: number; right_way: number; error_ratio: number | null; reliable_moves: number; reliable_right_way: number };
type History = { n: number; horizon_months: number; since: string | null; episodes: { start: string; end: string; driver_move: number }[] };
type PortfolioEffect = { implied_move: number; ci95: [number, number]; significant: boolean; past: Past; tested?: Tested | null;
  largest: { dataset_id: string; name: string; weight: number; contribution: number }[] };
type Scenario = { id: string; name: string; kind: string; driver_name: string; shock: number; shock_unit: string; relevance: number | null;
  relevance_label?: string; signals: { text: string }[]; impacts: Impact[]; clearest_effects: string[]; is_new?: boolean;
  history?: History | null; test?: TestRecord | null; portfolio?: PortfolioEffect | null };
type Brief = { id: string; name: string; relevance: number | null };
type Changes = { previous_scan_at: string; previous_as_of: string | null; new: Brief[]; dropped: Brief[];
  stronger: { id: string; name: string; from: number; to: number }[]; weaker: { id: string; name: string; from: number; to: number }[] };
type Scan = { as_of: string; considered: number; scenarios: Scenario[]; next_fomc: string | null; missing: string[]; method: { caveat: string };
  changes?: Changes | null; portfolio?: { name: string; holdings: number; excluded: string[]; tested: boolean } | null; portfolio_missing?: boolean };
type RunSummary = { run_id: string; query: string; status: string; title: string | null; created_at: string };
type Schedule = { schedule_id: string; name: string; cadence: string; next_run_at: string; config: { template?: string } };
type Custom = { driver: string; shock: string; name: string };

const KIND: Record<string, string> = { macro: "Macro", positioning: "Positioning", fundamentals: "Supply and demand", custom: "Yours" };

const signed = (v: number, unit: string, nd = 1) => `${v > 0 ? "+" : ""}${v.toFixed(nd)}${unit === "bp" ? " bp" : "%"}`;

function PastCell({ past, unit }: { past?: Past | null; unit: string }) {
  if (!past) return <>n/a</>;
  if (past.too_few) return <>too few</>;
  const range = `Ranged from ${signed(past.low!, unit)} to ${signed(past.high!, unit)} over ${past.n} past episodes`;
  return (
    <span title={range}>
      {signed(past.median!, unit)}{past.same_direction !== undefined ? ` · ${past.same_direction}/${past.n} same way` : ""}
      {past.disagrees && <span style={{ color: "var(--down)" }}> · went the other way</span>}
    </span>
  );
}

function HistoryLine({ h }: { h?: History | null }) {
  if (!h || !h.since) return null;
  const since = h.since.slice(0, 4);
  if (h.n === 0) return <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>No move this large within {h.horizon_months} months since {since}, so this has no precedent in the stored data.</p>;
  return (
    <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
      Moved this much within {h.horizon_months} months {h.n === 1 ? "once" : `${h.n} times`} since {since}, most recently by {h.episodes[h.n - 1].end.slice(0, 7)}.
    </p>
  );
}

function TestLine({ t }: { t?: TestRecord | null }) {
  if (!t) return null;
  const ratio = t.error_ratio;
  const change = ratio === null ? 0 : Math.round(Math.abs(1 - ratio) * 100);
  return (
    <p className="muted" style={{ fontSize: 12, margin: "0 0 8px" }}>
      <strong>Tested on past episodes:</strong> fitted only on earlier data, these sensitivities pointed the right way on {t.right_way} of {t.moves} market moves across {t.episodes} episodes
      {t.reliable_moves > 0 && ` (${t.reliable_right_way} of ${t.reliable_moves} for those that looked reliable at the time)`}.
      {ratio !== null && <> The median market's errors were <span style={ratio >= 1.05 ? { color: "var(--down)" } : undefined}>{change < 5 ? "about the same as" : `${change}% ${ratio < 1 ? "smaller" : "larger"} than`}</span> assuming no move.</>}
    </p>
  );
}

const testCell = (t?: Tested | null) => !t ? "n/a" : t.too_few ? "too few" : `${t.right_way}/${t.n}${t.error_ratio != null ? ` · ${t.error_ratio.toFixed(2)}×` : ""}`;
const testTitle = (t?: Tested | null) => !t || t.too_few ? "" : ` · Past test: right way ${t.right_way}/${t.n}${t.error_ratio != null ? `, error ${t.error_ratio.toFixed(2)}× assuming no move` : ""}`;

function ImpactBars({ s }: { s: Scenario }) {
  const rows = s.impacts.filter((i) => !i.is_driver).sort((a, b) => Number(b.significant) - Number(a.significant) || Math.abs(b.implied_move) - Math.abs(a.implied_move));
  const max = (unit: string) => Math.max(1e-9, ...rows.filter((r) => r.unit === unit).map((r) => Math.abs(r.implied_move)));
  return (
    <table style={{ fontSize: 12.5 }}>
      <thead><tr><th>Market</th><th /><th className="num">Effect</th><th className="num">95% range</th><th className="num">Past median</th></tr></thead>
      <tbody>{rows.map((r) => {
        const w = (Math.abs(r.implied_move) / max(r.unit)) * 50;
        const up = r.implied_move >= 0;
        return (
          <tr key={r.dataset_id} style={{ opacity: r.significant ? 1 : 0.5 }} title={(r.significant ? "Statistically significant (p ≤ 0.05)" : "Not statistically significant") + testTitle(r.tested)}>
            <td>{r.name}</td>
            <td style={{ width: "20%", minWidth: 80 }}>
              <div style={{ position: "relative", height: 8, background: "var(--panel-2)", borderRadius: 4 }}>
                <div style={{ position: "absolute", top: 0, height: 8, borderRadius: 4, left: up ? "50%" : `${50 - w}%`, width: `${w}%`, background: up ? "var(--up)" : "var(--down)" }} />
                <div style={{ position: "absolute", left: "50%", top: -2, width: 1, height: 12, background: "var(--line)" }} />
              </div>
            </td>
            <td className="num" style={{ whiteSpace: "nowrap" }}>{signed(r.implied_move, r.unit, 2)}</td>
            <td className="num muted" style={{ whiteSpace: "nowrap", fontSize: 11.5 }}>{r.ci95 ? `${r.ci95[0].toFixed(1)} to ${r.ci95[1].toFixed(1)}` : ""}{r.significant ? "" : " · not reliable"}</td>
            <td className="num muted" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}><PastCell past={r.past} unit={r.unit} /></td>
          </tr>
        );
      })}</tbody>
    </table>
  );
}

export default function Scenarios() {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [universe, setUniverse] = useState<string[]>(DEFAULT_UNIVERSE);
  const [add, setAdd] = useState("");
  const [custom, setCustom] = useState<Custom[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<ResearchRun | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [note, setNote] = useState<string | null>(null);
  const [portfolios, setPortfolios] = useState<{ portfolio_id: string; name: string }[]>([]);
  const [portfolioId, setPortfolioId] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const loadLists = useCallback(() => {
    api<RunSummary[]>(`/api/v1/research?limit=100&q=${encodeURIComponent("Scenario outlook")}`)
      .then((r) => { const mine = r.filter((x) => x.query === QUERY || (x.title ?? "").startsWith("Scenario Outlook")); setRuns(mine); setRunId((id) => id ?? mine.find((x) => x.status === "complete")?.run_id ?? null); })
      .catch(setError);
    api<Schedule[]>("/api/v1/schedules").then((s) => setSchedules(s.filter((x) => x.config?.template === "scenario_outlook"))).catch(() => {});
  }, []);
  useEffect(() => {
    api<Instrument[]>("/api/v1/instruments").then((all) => setInstruments(all.filter((i) => i.has_source && (i.kind === "market" || i.kind === "macro")))).catch(setError);
    api<{ portfolio_id: string; name: string }[]>("/api/v1/portfolios").then(setPortfolios).catch(() => {});
    loadLists();
  }, [loadLists]);

  const poll = useCallback(async (id: string) => {
    clearTimeout(timer.current);
    try {
      const r = await api<ResearchRun>(`/api/v1/research/${id}`);
      setRun(r);
      if (r.status === "queued" || r.status === "running") timer.current = setTimeout(() => poll(id), 1500);
      else loadLists();
    } catch (e) { setError(e); }
  }, [loadLists]);
  useEffect(() => { if (runId) poll(runId); return () => clearTimeout(timer.current); }, [runId, poll]);

  const options = () => ({
    universe,
    custom: custom.filter((c) => c.driver && Number(c.shock)).map((c) => ({ driver: c.driver, shock: Number(c.shock), name: c.name })),
    ...(portfolioId ? { portfolio_id: portfolioId } : {}),
  });
  const runNow = async () => {
    setError(null); setNote(null);
    try {
      const r = await api<{ run_id: string }>("/api/v1/research", { method: "POST", body: JSON.stringify({
        query: QUERY, report: { template: "scenario_outlook", formats: ["html", "xlsx"] }, scenarios: options(), no_cache: true }) });
      setRunId(r.run_id);
    } catch (e) { setError(e); }
  };
  const weekly = async () => {
    setError(null); setNote(null);
    try {
      await api("/api/v1/schedules", { method: "POST", body: JSON.stringify({ name: "Weekly scenario outlook", query: QUERY, cadence: "weekly",
        template: "scenario_outlook", scenarios: options() }) });
      setNote("The scenario agent will run every Monday after the data refresh. When it finds new or stronger scenarios you'll get an email (and a scenarios.changed webhook, if you have one).");
      loadLists();
    } catch (e) { setError(e); }
  };
  const removeSchedule = async (id: string) => { await api(`/api/v1/schedules/${id}`, { method: "DELETE" }).catch(setError); loadLists(); };

  const name = (id: string) => instruments.find((i) => i.dataset_id === id)?.name ?? id;
  const scan = run?.result?.evidence.find((e) => e.tool === "scan_scenarios")?.result as Scan | undefined;
  const active = run && (run.status === "queued" || run.status === "running");
  const tested = (scan?.scenarios ?? []).filter((s) => s.portfolio).sort((a, b) => a.portfolio!.implied_move - b.portfolio!.implied_move);

  return (
    <>
      <PageHeader title="Scenario Agent" subtitle="Scans all stored data for the scenarios it points to (rates, the dollar, oil, volatility, speculative positioning, USDA supply and demand, plus your own) and shows what each would mean for other markets, based on how they have moved together over 10 years and what they did the last times it happened. Test any saved portfolio too." />
      <ErrorNote error={error} />
      {note && <div className="note" style={{ marginBottom: 14 }}>{note}</div>}

      <div className="grid grid-2" style={{ marginBottom: 14 }}>
        <Card title="Markets to assess">
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {universe.map((d) => (
              <button key={d} className="secondary" style={{ fontSize: 12, padding: "3px 8px" }} onClick={() => setUniverse(universe.filter((x) => x !== d))} aria-label={`Remove ${name(d)}`}>
                {name(d)} ×
              </button>
            ))}
          </div>
          <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "nowrap" }}>
            <select value={add} onChange={(e) => setAdd(e.target.value)} aria-label="Add a market">
              <option value="">Add a market…</option>
              {instruments.filter((i) => !universe.includes(i.dataset_id)).map((i) => <option key={i.dataset_id} value={i.dataset_id}>{i.name}</option>)}
            </select>
            <button className="secondary" disabled={!add || universe.length >= 15} onClick={() => { setUniverse([...universe, add]); setAdd(""); }}>Add</button>
          </div>
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>Up to 15. <a href="#" onClick={(e) => { e.preventDefault(); setUniverse(DEFAULT_UNIVERSE); }}>Reset to the default set</a>.</p>
        </Card>
        <Card title="Your own scenarios (optional)">
          {custom.map((c, i) => (
            <div key={i} className="row" style={{ gap: 6, marginBottom: 6, flexWrap: "nowrap" }}>
              <select value={c.driver} onChange={(e) => setCustom(custom.map((x, j) => j === i ? { ...x, driver: e.target.value } : x))} aria-label="What moves">
                <option value="">What moves…</option>
                {instruments.map((x) => <option key={x.dataset_id} value={x.dataset_id}>{x.name}</option>)}
              </select>
              <input value={c.shock} onChange={(e) => setCustom(custom.map((x, j) => j === i ? { ...x, shock: e.target.value } : x))} placeholder="+10" inputMode="decimal" aria-label="By how much" style={{ width: 80 }} />
              <span className="muted" style={{ fontSize: 12 }}>{instruments.find((x) => x.dataset_id === c.driver)?.unit === "percent" ? "bp" : "%"}</span>
              <input value={c.name} onChange={(e) => setCustom(custom.map((x, j) => j === i ? { ...x, name: e.target.value } : x))} placeholder="Name (optional)" aria-label="Scenario name" />
              <button className="secondary" onClick={() => setCustom(custom.filter((_, j) => j !== i))} aria-label="Remove scenario">×</button>
            </div>
          ))}
          {custom.length < 5 && <button className="secondary" onClick={() => setCustom([...custom, { driver: "", shock: "", name: "" }])}>Add a scenario</button>}
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>For example, the Brazilian real weakens 10% (USD/BRL +10%), or the 10-year yield rises 50 bp.</p>
        </Card>
      </div>

      <div className="row" style={{ gap: 8, marginBottom: 14, flexWrap: "wrap" }}>
        {portfolios.length > 0 && (
          <select value={portfolioId} onChange={(e) => setPortfolioId(e.target.value)} aria-label="Portfolio to test" style={{ width: "auto" }}>
            <option value="">No portfolio</option>
            {portfolios.map((x) => <option key={x.portfolio_id} value={x.portfolio_id}>Test on: {x.name}</option>)}
          </select>
        )}
        <button onClick={runNow} disabled={!!active || universe.length === 0}>{active ? "Scanning…" : "Run the scenario agent now"}</button>
        <button className="secondary" onClick={weekly} disabled={universe.length === 0}>Run it every week</button>
        {schedules.map((s) => (
          <span key={s.schedule_id} className="muted" style={{ fontSize: 12 }}>
            Weekly · next {s.next_run_at.slice(0, 10)} <a href="#" onClick={(e) => { e.preventDefault(); removeSchedule(s.schedule_id); }}>stop</a>
          </span>
        ))}
      </div>

      {active && <Card><span className="muted">{run?.progress?.at(-1)?.message ?? "Scanning the data…"}</span></Card>}
      {run?.status === "failed" && <div className="note note-error">The scan failed: {run.error}</div>}

      {scan && !active && (
        <>
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 10, flexWrap: "wrap" }}>
            <div className="muted" style={{ fontSize: 13 }}>
              Data as of {scan.as_of} · {scan.considered} candidates checked · {scan.scenarios.length} shown{scan.next_fomc ? ` · next FOMC ${scan.next_fomc}` : ""}
              {run?.result?.verification.passed ? " · all figures verified" : ""}
            </div>
            <Link href={`/research?run=${run!.run_id}`}>Read the full brief</Link>
          </div>
          {run?.result?.engine_note && <div className="note" style={{ marginBottom: 12, fontSize: 12.5 }}>{run.result.engine_note}</div>}
          {scan.changes && (
            <Card title={`What changed since ${scan.changes.previous_scan_at.slice(0, 10)}`}>
              {scan.changes.new.length + scan.changes.stronger.length + scan.changes.weaker.length + scan.changes.dropped.length === 0
                ? <p className="muted" style={{ margin: 0 }}>Nothing changed: the same scenarios, with the same signals.</p>
                : (
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                    {scan.changes.new.map((x) => <li key={`n${x.id}`}><strong>New:</strong> {x.name}</li>)}
                    {scan.changes.stronger.map((x) => <li key={`s${x.id}`}><strong>Stronger:</strong> {x.name} ({x.from} → {x.to} signals)</li>)}
                    {scan.changes.weaker.map((x) => <li key={`w${x.id}`}>Weaker: {x.name} ({x.from} → {x.to} signals)</li>)}
                    {scan.changes.dropped.map((x) => <li key={`d${x.id}`} className="muted">No longer signalled: {x.name}</li>)}
                  </ul>
                )}
            </Card>
          )}
          {scan.portfolio_missing && <div className="note" style={{ marginTop: 12, fontSize: 12.5 }}>The portfolio chosen for this scan no longer exists, so it was not tested.</div>}
          {scan.portfolio && !scan.portfolio.tested && <div className="note" style={{ marginTop: 12, fontSize: 12.5 }}>{scan.portfolio.name} could not be tested: its holdings have too little shared monthly history.</div>}
          {tested.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <Card title={`Effect on ${scan.portfolio!.name}`}>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ fontSize: 12.5 }}>
                    <thead><tr><th>Scenario</th><th className="num">Effect</th><th className="num">95% range</th><th className="num">Past median</th><th className="num" title="Fitted only on data from before each past episode">Past test</th><th>Largest contributors</th></tr></thead>
                    <tbody>{tested.map((s) => {
                      const f = s.portfolio!;
                      return (
                        <tr key={s.id} style={{ opacity: f.significant ? 1 : 0.5 }} title={f.significant ? "Statistically significant (p ≤ 0.05)" : "Not statistically significant"}>
                          <td>{s.name}</td>
                          <td className="num" style={{ whiteSpace: "nowrap", color: f.significant ? (f.implied_move < 0 ? "var(--down)" : "var(--up)") : undefined }}>{signed(f.implied_move, "pct", 2)}</td>
                          <td className="num muted" style={{ whiteSpace: "nowrap" }}>{f.ci95[0].toFixed(1)} to {f.ci95[1].toFixed(1)}{f.significant ? "" : " · not reliable"}</td>
                          <td className="num muted"><PastCell past={f.past} unit="pct" /></td>
                          <td className="num muted" style={{ whiteSpace: "nowrap" }} title="Right direction, and errors compared with assuming no move">{testCell(f.tested)}</td>
                          <td className="muted">{f.largest.map((c) => `${c.name} ${signed(c.contribution, "pct")}`).join(", ")}</td>
                        </tr>
                      );
                    })}</tbody>
                  </table>
                </div>
                <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
                  {scan.portfolio!.holdings} holdings, measured as one monthly series at current weights, so diversification between them is included. Past test: how often an effect fitted on earlier data pointed the right way, and its errors as a multiple of assuming no move (below 1× is better). It uses today's weights, so it shows how this mix would have behaved, not a real track record.
                  {scan.portfolio!.excluded.length > 0 && ` Excluded (no usable history): ${scan.portfolio!.excluded.join(", ")}.`}
                </p>
              </Card>
            </div>
          )}
          <div style={{ height: 14 }} />
          <div style={{ display: "grid", gap: 14 }}>
            {scan.scenarios.map((s) => (
              <Card key={s.id} title={s.name} actions={<span className="row" style={{ gap: 6 }}>{s.is_new && <span className="badge badge-current">New</span>}<span className={`badge ${s.relevance && s.relevance >= 2 ? "badge-current" : "badge-delayed"}`}>
                {s.kind === "custom" ? "Yours" : `${KIND[s.kind] ?? s.kind} · ${s.relevance_label}`}</span></span>}>
                <ul style={{ margin: "0 0 10px", paddingLeft: 18, fontSize: 13 }}>
                  {s.signals.map((g, i) => <li key={i}>{g.text}</li>)}
                  {s.signals.length === 0 && <li className="muted">No current signal; included as a standard stress test.</li>}
                </ul>
                <HistoryLine h={s.history} />
                <TestLine t={s.test} />
                <div style={{ overflowX: "auto" }}><ImpactBars s={s} /></div>
              </Card>
            ))}
          </div>
          <p className="muted" style={{ fontSize: 12 }}>{scan.method.caveat}</p>
          {scan.missing.length > 0 && <p className="muted" style={{ fontSize: 12 }}>Not assessed (no stored data): {scan.missing.map(name).join(", ")}.</p>}
        </>
      )}
      {!scan && !active && runs.length === 0 && <Card><p className="muted" style={{ margin: 0 }}>No scans yet. Run the scenario agent to see which scenarios the data points to.</p></Card>}

      {runs.length > 1 && (
        <Card title="Earlier scans">
          <ul className="list-tight">{runs.slice(0, 12).map((r) => (
            <li key={r.run_id}><a href="#" onClick={(e) => { e.preventDefault(); setRunId(r.run_id); }}>{r.created_at.slice(0, 16).replace("T", " ")} UTC</a>
              <span className="muted"> · {r.status}{r.run_id === runId ? " · shown" : ""}</span></li>
          ))}</ul>
        </Card>
      )}
    </>
  );
}
