"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, type Instrument } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Trigger = { type: string; [k: string]: number | string };
type AgentEvent = { event_id: string; at: string; status: string; run_id: string | null; findings: { message: string }[]; read: boolean };
type Agent = {
  agent_id: string; name: string; watch: string[]; triggers: Trigger[]; action: { template: string; query: string; engine?: string };
  cooldown_hours: number; notify_email: boolean; status: string; last_checked_at: string | null; last_fired_at: string | null; events: AgentEvent[];
};
type TriggerSpec = Record<string, { label: string; param: string; default: number | string; help: string; kind?: "text"; placeholder?: string; extra?: Record<string, number> }>;

export default function Agents({ instruments }: { instruments: Instrument[] }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [specs, setSpecs] = useState<TriggerSpec>({});
  const [templates, setTemplates] = useState<{ key: string; name: string }[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [name, setName] = useState("Commodity watch");
  const [watch, setWatch] = useState<string[]>(["CMD.SUGAR.ISA", "CMD.CRUDE.BRENT", "CMD.COPPER.LME"]);
  const [triggers, setTriggers] = useState<Trigger[]>([{ type: "crowded_positioning", extreme: 90 }, { type: "pct_move", abs_pct: 10, window_days: 30 }]);
  const [template, setTemplate] = useState("commodity_deep_dive");
  const [query, setQuery] = useState("Explain what triggered this alert and what it means for the next three months.");
  const [notify, setNotify] = useState(false);

  const load = useCallback(() => {
    api<{ agents: Agent[] }>("/api/v1/agents").then((r) => setAgents(r.agents)).catch(setError);
  }, []);
  useEffect(() => {
    load();
    api<TriggerSpec>("/api/v1/agents/triggers").then(setSpecs).catch(() => {});
    api<{ key: string; name: string }[]>("/api/v1/report-templates").then(setTemplates).catch(() => {});
    api("/api/v1/agents/inbox/read", { method: "POST" }).catch(() => {});
  }, [load]);

  const create = async () => {
    setError(null);
    try {
      await api("/api/v1/agents", { method: "POST", body: JSON.stringify({ name, watch, triggers, action: { template, query }, notify_email: notify }) });
      load();
    } catch (e) { setError(e); }
  };
  const check = async (id: string) => {
    setMsg("Checking…");
    try {
      const r = await api<{ fired: boolean; suppressed?: boolean; run_id?: string; findings: { message: string }[] }>(`/api/v1/agents/${id}/check`, { method: "POST" });
      setMsg(r.fired ? `Fired: ${r.findings.map((f) => f.message).join(" · ")}. Research started.`
        : r.suppressed ? `Conditions met but in cooldown: ${r.findings.map((f) => f.message).join(" · ")}`
        : "No trigger conditions met right now.");
      load();
    } catch (e) { setMsg(null); setError(e); }
  };
  const nameOf = (id: string) => instruments.find((i) => i.dataset_id === id)?.name.split(" (")[0] ?? id;

  return (
    <div className="split">
      <Card title="New monitoring agent">
        <div className="field"><label htmlFor="an">Name</label><input id="an" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div className="field">
          <label>Watch</label>
          <select multiple value={watch} onChange={(e) => setWatch([...e.target.selectedOptions].map((o) => o.value))} style={{ height: 120 }} aria-label="Watched instruments">
            {instruments.map((i) => <option key={i.dataset_id} value={i.dataset_id}>{i.name}</option>)}
          </select>
          <p className="muted" style={{ fontSize: 11.5, margin: "4px 0 0" }}>⌘/Ctrl-click to select several. Agents with only official-release or central-bank triggers need no instruments.</p>
        </div>
        <label style={{ marginTop: 12 }}>Triggers (any fires the agent)</label>
        {triggers.map((t, i) => {
          const spec = specs[t.type];
          return (
            <div className="row" key={i} style={{ flexWrap: spec?.kind === "text" ? "wrap" : "nowrap", marginBottom: 6 }}>
              <select value={t.type} onChange={(e) => {
                const sp = specs[e.target.value];
                setTriggers(triggers.map((x, j) => (j === i ? { type: e.target.value, [sp.param]: sp.default, ...(sp.extra ?? {}) } : x)));
              }}>
                {Object.entries(specs).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
              </select>
              {spec && spec.kind === "text" && <input type="text" style={{ minWidth: 0, flex: "1 1 180px" }} value={t[spec.param] as string} title={spec.help}
                placeholder={spec.placeholder ?? "FOMC statement, GDP"} onChange={(e) => setTriggers(triggers.map((x, j) => (j === i ? { ...x, [spec.param]: e.target.value } : x)))} aria-label={spec.param} />}
              {spec && spec.kind !== "text" && <input type="number" step="any" style={{ width: 64, flex: "none" }} value={t[spec.param] as number} title={spec.help}
                onChange={(e) => setTriggers(triggers.map((x, j) => (j === i ? { ...x, [spec.param]: parseFloat(e.target.value) } : x)))} aria-label={spec.param} />}
              <button className="secondary" onClick={() => setTriggers(triggers.filter((_, j) => j !== i))} aria-label="Remove trigger">×</button>
            </div>
          );
        })}
        <button className="secondary" onClick={() => setTriggers([...triggers, { type: "anomaly", z: 3.5 }])}>Add trigger</button>
        <div className="field" style={{ marginTop: 12 }}>
          <label>When it fires, run</label>
          <select value={template} onChange={(e) => setTemplate(e.target.value)}>{templates.map((t) => <option key={t.key} value={t.key}>{t.name}</option>)}</select>
        </div>
        <div className="field"><label htmlFor="aq">Research question</label><textarea id="aq" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
        <label className="row" style={{ fontWeight: 400, marginTop: 8 }}><input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} style={{ width: "auto" }} /> Email workspace owners when it fires</label>
        <button style={{ marginTop: 12 }} onClick={create}>Create agent</button>
        <p className="muted" style={{ fontSize: 12 }}>Agents are checked automatically after each data refresh (release triggers when new official releases are indexed); a 24-hour cooldown prevents repeat reports.</p>
        <ErrorNote error={error} />
      </Card>
      <div className="stack">
        {msg && <div className="note">{msg}</div>}
        {agents.length === 0 && <Card><span className="muted">No agents yet.</span></Card>}
        {agents.map((a) => (
          <Card key={a.agent_id} title={<>{a.name} {a.status !== "active" && <span className="badge badge-no_data">{a.status}</span>}</>}
            actions={
              <div className="row">
                <button className="secondary" onClick={() => check(a.agent_id)}>Check now</button>
                <button className="secondary" onClick={() => api(`/api/v1/agents/${a.agent_id}/${a.status === "active" ? "pause" : "resume"}`, { method: "POST" }).then(load)}>{a.status === "active" ? "Pause" : "Resume"}</button>
                <button className="secondary" onClick={() => api(`/api/v1/agents/${a.agent_id}`, { method: "DELETE" }).then(load)}>Delete</button>
              </div>
            }>
            <div className="muted" style={{ fontSize: 12.5 }}>
              {a.watch.length ? <>Watches {a.watch.map(nameOf).join(", ")}</> : "Watches official releases"} · triggers: {a.triggers.map((t) => (specs[t.type]?.label ?? t.type) + (specs[t.type]?.kind === "text" ? ` (${t[specs[t.type].param]})` : "")).join(", ")} · runs {templates.find((t) => t.key === a.action.template)?.name ?? a.action.template}
            </div>
            <div className="muted" style={{ fontSize: 12 }}>Last checked {a.last_checked_at?.replace("T", " ").slice(0, 16) ?? "never"} · last fired {a.last_fired_at?.replace("T", " ").slice(0, 16) ?? "never"}</div>
            <ul className="list-tight" style={{ marginTop: 6 }}>
              {a.events.map((e) => (
                <li key={e.event_id} style={{ fontSize: 12.5 }}>
                  <span className={e.status === "fired" ? "up" : "muted"}>{e.status === "fired" ? "⚑ fired" : "suppressed"}</span>{" "}
                  <span className="muted">{e.at.replace("T", " ").slice(0, 16)}</span> — {e.findings.map((f) => f.message).join(" · ")}
                  {e.run_id && <> · <Link href={`/research?run=${e.run_id}`}>report</Link></>}
                </li>
              ))}
            </ul>
          </Card>
        ))}
      </div>
    </div>
  );
}
