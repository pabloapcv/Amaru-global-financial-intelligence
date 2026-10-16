"use client";

import { useCallback, useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, fmt, type Instrument } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type Holding = { dataset_id: string; weight: number };
type Portfolio = { portfolio_id: string; name: string; holdings: Holding[]; updated_at: string };
type Risk = {
  frequency: string;
  n_observations: number;
  start: string;
  end: string;
  gross_exposure: number;
  net_exposure: number;
  annualized_vol_pct: number;
  diversification_ratio: number;
  annualized_return_pct: number;
  max_drawdown_pct: number;
  var: Record<string, number | string>;
  holdings: { dataset_id: string; name: string; weight: number; annualized_vol_pct: number; risk_contribution_pct: number }[];
  correlation: { labels: string[]; matrix: number[][] };
  factor_exposures: { r_squared: number; betas: { factor: string; beta: number; p_value: number }[] } | null;
  stress_tests: { name: string; start: string; end: string; portfolio_return_pct: number | null; weight_coverage_pct: number; missing_holdings: string[] }[];
  cumulative: { date: string; value: number }[];
};

const EMPTY: Holding[] = [{ dataset_id: "CMD.CRUDE.BRENT", weight: 0.3 }, { dataset_id: "CMD.COPPER.LME", weight: 0.3 }, { dataset_id: "EQ.US.SP500", weight: 0.4 }];

function Tile({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return <div className="card quote"><div className="sub">{label}</div><div className={`value ${tone ?? ""}`} style={{ fontSize: 20 }}>{value}</div></div>;
}

function corrColor(v: number) {
  const a = Math.min(1, Math.abs(v));
  return v >= 0 ? `rgba(76,141,255,${a * 0.55})` : `rgba(240,96,93,${a * 0.55})`;
}

export default function PortfolioPage() {
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [allInstruments, setAllInstruments] = useState<Instrument[]>([]);
  const [list, setList] = useState<Portfolio[]>([]);
  const [current, setCurrent] = useState<Portfolio | null>(null);
  const [name, setName] = useState("My portfolio");
  const [holdings, setHoldings] = useState<Holding[]>(EMPTY);
  const [risk, setRisk] = useState<Risk | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const ps = await api<Portfolio[]>("/api/v1/portfolios");
    setList(ps);
    return ps;
  }, []);

  useEffect(() => {
    api<Instrument[]>("/api/v1/instruments")
      .then((all) => {
        setAllInstruments(all);
        setInstruments(all.filter((i) => i.kind === "market" && i.has_source && i.unit !== "percent"));
      })
      .catch(setError);
    load().then((ps) => ps[0] && select(ps[0])).catch(setError);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const select = async (p: Portfolio) => {
    setCurrent(p);
    setName(p.name);
    setHoldings(p.holdings);
    setRisk(null);
    setError(null);
    try {
      setRisk((await api<{ risk: Risk }>(`/api/v1/portfolios/${p.portfolio_id}/risk`)).risk);
    } catch (e) { setError(e); }
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = JSON.stringify({ name, holdings });
      const saved = current
        ? await api<Portfolio>(`/api/v1/portfolios/${current.portfolio_id}`, { method: "PUT", body })
        : await api<Portfolio>("/api/v1/portfolios", { method: "POST", body });
      await load();
      await select(saved);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  const remove = async () => {
    if (!current) return;
    await api(`/api/v1/portfolios/${current.portfolio_id}`, { method: "DELETE" });
    setCurrent(null); setRisk(null); setName("My portfolio"); setHoldings(EMPTY);
    load();
  };

  const nameOf = (id: string) => allInstruments.find((i) => i.dataset_id === id)?.name ?? id;
  const net = holdings.reduce((a, h) => a + (Number(h.weight) || 0), 0);

  return (
    <>
      <PageHeader title="Portfolio risk" subtitle="Volatility, value at risk, risk contributions, factor exposures and historical stress tests for a long/short book." />
      <div className="split">
        <div className="stack">
          <Card title="Portfolios" actions={<button className="secondary" onClick={() => { setCurrent(null); setRisk(null); setName("New portfolio"); setHoldings(EMPTY); }}>New</button>}>
            <ul className="list-tight">
              {list.map((p) => (
                <li key={p.portfolio_id}>
                  <a href="#" onClick={(e) => { e.preventDefault(); select(p); }} style={{ fontWeight: current?.portfolio_id === p.portfolio_id ? 700 : 400 }}>{p.name}</a>
                  <div className="muted" style={{ fontSize: 11.5 }}>{p.holdings.length} holdings</div>
                </li>
              ))}
              {list.length === 0 && <li className="muted">No saved portfolios yet.</li>}
            </ul>
          </Card>
          <Card title={current ? "Edit portfolio" : "New portfolio"}>
            <div className="field"><label htmlFor="pname">Name</label><input id="pname" value={name} onChange={(e) => setName(e.target.value)} /></div>
            <label style={{ marginTop: 12 }}>Holdings (weight = fraction of capital; negative = short)</label>
            {holdings.map((h, i) => (
              <div className="row" key={i} style={{ flexWrap: "nowrap", marginBottom: 6 }}>
                <select value={h.dataset_id} onChange={(e) => setHoldings(holdings.map((x, j) => (j === i ? { ...x, dataset_id: e.target.value } : x)))} aria-label={`Holding ${i + 1}`}>
                  {instruments.map((ins) => <option key={ins.dataset_id} value={ins.dataset_id}>{ins.name}</option>)}
                </select>
                <input type="number" step="0.05" value={h.weight} style={{ width: 80 }} aria-label={`Weight ${i + 1}`}
                  onChange={(e) => setHoldings(holdings.map((x, j) => (j === i ? { ...x, weight: parseFloat(e.target.value) } : x)))} />
                <button className="secondary" onClick={() => setHoldings(holdings.filter((_, j) => j !== i))} aria-label="Remove holding">×</button>
              </div>
            ))}
            <div className="row" style={{ justifyContent: "space-between" }}>
              <button className="secondary" onClick={() => setHoldings([...holdings, { dataset_id: instruments[0]?.dataset_id ?? "CMD.CRUDE.WTI", weight: 0.1 }])}>Add holding</button>
              <span className="muted" style={{ fontSize: 12 }}>Net {fmt(net, 2)}</span>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <button onClick={save} disabled={busy}>{busy ? "Analyzing…" : "Save & analyze"}</button>
              {current && <button className="secondary" onClick={remove}>Delete</button>}
            </div>
            <div style={{ marginTop: 10 }}><ErrorNote error={error} /></div>
          </Card>
        </div>

        <div className="stack">
          {!risk ? (
            <Card><span className="muted">{current ? "Analyzing…" : "Save a portfolio to see its risk."}</span></Card>
          ) : (
            <>
              <div className="grid grid-4">
                <Tile label="Annualized volatility" value={`${fmt(risk.annualized_vol_pct)}%`} />
                <Tile label={`VaR 95% · ${risk.var.horizon}`} value={`${fmt(risk.var.historical_95_pct as number)}%`} tone="down" />
                <Tile label="Expected shortfall 95%" value={`${fmt(risk.var.cvar_95_pct as number)}%`} tone="down" />
                <Tile label="Max drawdown" value={`${fmt(risk.max_drawdown_pct)}%`} tone="down" />
              </div>
              <Card title={`Growth of 1 · ${risk.frequency} · ${risk.start} → ${risk.end}`}>
                <Chart spec={{ type: "line", title: "Cumulative", unit: "growth of 1", series: [{ name: name, points: risk.cumulative }] }} height={220} />
                <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
                  Annualized return {fmt(risk.annualized_return_pct)}% · diversification ratio {fmt(risk.diversification_ratio, 2)} · gross {fmt(risk.gross_exposure, 2)} / net {fmt(risk.net_exposure, 2)} · {risk.n_observations} observations
                </p>
              </Card>
              <div className="grid grid-2">
                <Card title="Risk contribution">
                  <table>
                    <tbody>
                      {[...risk.holdings].sort((a, b) => b.risk_contribution_pct - a.risk_contribution_pct).map((h) => (
                        <tr key={h.dataset_id}>
                          <td style={{ fontSize: 12.5 }}>{h.name.split(" (")[0]}<div className="muted" style={{ fontSize: 11 }}>weight {fmt(h.weight, 2)} · vol {fmt(h.annualized_vol_pct, 1)}%</div></td>
                          <td style={{ width: "35%" }}>
                            <div style={{ height: 6, background: "var(--panel-2)", borderRadius: 3 }}>
                              <div style={{ width: `${Math.max(0, Math.min(100, h.risk_contribution_pct))}%`, height: 6, borderRadius: 3, background: "var(--accent)" }} />
                            </div>
                          </td>
                          <td className="num">{fmt(h.risk_contribution_pct, 1)}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
                <Card title="Historical stress tests">
                  <table>
                    <tbody>
                      {risk.stress_tests.map((s) => (
                        <tr key={s.name}>
                          <td style={{ fontSize: 12.5 }}>{s.name}<div className="muted" style={{ fontSize: 11 }}>{s.start} → {s.end}{s.weight_coverage_pct < 100 && s.portfolio_return_pct !== null ? ` · covers ${fmt(s.weight_coverage_pct, 0)}% of weight` : ""}</div></td>
                          <td className={`num ${(s.portfolio_return_pct ?? 0) < 0 ? "down" : "up"}`}>{s.portfolio_return_pct === null ? <span className="muted">no data</span> : `${fmt(s.portfolio_return_pct, 1)}%`}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              </div>
              <div className="grid grid-2">
                <Card title={`Factor exposures${risk.factor_exposures ? ` · R² ${fmt(risk.factor_exposures.r_squared, 2)}` : ""}`}>
                  {risk.factor_exposures ? (
                    <table>
                      <tbody>
                        {risk.factor_exposures.betas.map((b) => (
                          <tr key={b.factor}>
                            <td>{nameOf(b.factor)}</td>
                            <td className="num">{fmt(b.beta, 3)}</td>
                            <td className="num muted">{b.p_value < 0.05 ? "significant" : `p ${fmt(b.p_value, 2)}`}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : <span className="muted">Not enough overlapping history.</span>}
                </Card>
                <Card title="Correlation">
                  <table style={{ fontSize: 11.5 }}>
                    <thead><tr><th />{risk.correlation.labels.map((l) => <th key={l} className="num" title={nameOf(l)}>{l.split(".").slice(-1)[0]}</th>)}</tr></thead>
                    <tbody>
                      {risk.correlation.matrix.map((row, i) => (
                        <tr key={i}>
                          <td title={nameOf(risk.correlation.labels[i])}>{risk.correlation.labels[i].split(".").slice(-1)[0]}</td>
                          {row.map((v, j) => <td key={j} className="num" style={{ background: i === j ? undefined : corrColor(v) }}>{fmt(v, 2)}</td>)}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Card>
              </div>
              <p className="muted" style={{ fontSize: 12 }}>Reference and benchmark prices, not tradable quotes; no transaction costs, carry or roll. Stress tests apply each holding's move over the window to today's weights.</p>
            </>
          )}
        </div>
      </div>
    </>
  );
}
