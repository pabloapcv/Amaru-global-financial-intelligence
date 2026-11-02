"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Instrument } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";
import Agents from "@/components/Agents";
import CentralBankTone from "@/components/CentralBankTone";
import Releases from "@/components/Releases";

type AlertRow = {
  alert_id: string;
  name: string;
  rule: Record<string, any>;
  status: string;
  last_evaluated_at: string | null;
  last_triggered_at: string | null;
  events: { triggered_at: string; message: string }[];
  notify_email: boolean;
  mine: boolean;
};

const BANK: Record<string, string> = { fed: "Fed", ecb: "ECB", any: "Fed or ECB" };

function describe(rule: Record<string, any>) {
  if (rule.type === "threshold") return `${rule.dataset_id} ${rule.op} ${rule.value}`;
  if (rule.type === "pct_change") return `${rule.dataset_id} moves ≥ ${rule.abs_pct}% over ${rule.window_days ?? 30}d`;
  if (rule.type === "anomaly") return `${rule.dataset_id} |robust z| ≥ ${rule.z ?? 3.5}`;
  if (rule.type === "central_bank") return `${BANK[rule.bank] ?? rule.bank} statement: rate change or tone shift ≥ ${rule.min_stance ?? 1}`;
  return JSON.stringify(rule);
}

export default function Intelligence() {
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [type, setType] = useState("pct_change");
  const [dataset, setDataset] = useState("CMD.CRUDE.BRENT");
  const [bank, setBank] = useState("any");
  const [value, setValue] = useState(10);
  const [op, setOp] = useState(">");
  const [fired, setFired] = useState<string[] | null>(null);
  const [notify, setNotify] = useState(true);
  const [tab, setTab] = useState<"agents" | "alerts" | "releases">("agents");

  const load = useCallback(() => api<AlertRow[]>("/api/v1/alerts").then(setAlerts).catch(setError), []);
  useEffect(() => {
    load();
    api<Instrument[]>("/api/v1/instruments").then((all) => setInstruments(all.filter((i) => i.has_source && (i.kind === "market" || i.dataset_id.startsWith("US."))))).catch(setError);
  }, [load]);

  const create = async () => {
    setError(null);
    const central = type === "central_bank";
    const rule: Record<string, unknown> = central ? { type, bank, min_stance: value } : { type, dataset_id: dataset };
    if (type === "threshold") Object.assign(rule, { op, value });
    if (type === "pct_change") Object.assign(rule, { abs_pct: value, window_days: 30 });
    if (type === "anomaly") Object.assign(rule, { z: value });
    const name = central ? `${BANK[bank]} tone shift` : `${instruments.find((i) => i.dataset_id === dataset)?.name.split(" (")[0]} — ${type.replace("_", " ")}`;
    try {
      await api("/api/v1/alerts", { method: "POST", body: JSON.stringify({ name, rule, notify_email: notify }) });
      load();
    } catch (e) {
      setError(e);
    }
  };

  const evaluate = async () => {
    const r = await api<{ fired: { message: string }[] }>("/api/v1/alerts/evaluate", { method: "POST" });
    setFired(r.fired.map((f) => f.message));
    load();
  };

  return (
    <>
      <PageHeader
        title="Intelligence"
        subtitle="Agents and alerts are evaluated after every data refresh; official releases (Fed, BEA, CFTC, USDA AMS, ECB, Bank of England, Banco Central do Brasil) are indexed every 6 hours."
        actions={
          <div className="row">
            <div className="seg">
              <button className={tab === "agents" ? "on" : ""} onClick={() => setTab("agents")}>Agents</button>
              <button className={tab === "alerts" ? "on" : ""} onClick={() => setTab("alerts")}>Alerts</button>
              <button className={tab === "releases" ? "on" : ""} onClick={() => setTab("releases")}>Releases</button>
            </div>
            {tab === "alerts" && <button className="secondary" onClick={evaluate}>Evaluate all now</button>}
          </div>
        }
      />
      {tab === "agents" && <Agents instruments={instruments} />}
      {tab === "releases" && <><CentralBankTone /><div style={{ height: 14 }} /><Releases /></>}
      {tab === "alerts" && <>
      <ErrorNote error={error} />
      {fired && <div className={`note ${fired.length ? "note-warn" : ""}`} style={{ marginBottom: 14 }}>{fired.length ? fired.join(" · ") : "No alerts fired."}</div>}
      <div className="split">
        <Card title="New alert">
          <div className="field">
            <label>Rule</label>
            <select value={type} onChange={(e) => { setType(e.target.value); setValue(e.target.value === "anomaly" ? 3.5 : e.target.value === "central_bank" ? 1 : 10); }}>
              <option value="pct_change">% move over 30 days</option>
              <option value="threshold">Level threshold</option>
              <option value="anomaly">Unusual move (robust z)</option>
              <option value="central_bank">Central-bank statement</option>
            </select>
          </div>
          {type === "central_bank" ? (
            <>
              <div className="field">
                <label>Bank</label>
                <select value={bank} onChange={(e) => setBank(e.target.value)}>
                  <option value="any">Fed or ECB</option><option value="fed">Federal Reserve</option><option value="ecb">European Central Bank</option>
                </select>
              </div>
              <div className="field">
                <label>Fires on</label>
                <select value={value} onChange={(e) => setValue(+e.target.value)}>
                  <option value={1}>A rate change or any tone shift</option><option value={2}>A rate change or a large tone shift</option>
                </select>
              </div>
            </>
          ) : (
            <>
              <div className="field">
                <label>Dataset</label>
                <select value={dataset} onChange={(e) => setDataset(e.target.value)}>
                  {instruments.map((i) => <option key={i.dataset_id} value={i.dataset_id}>{i.name}</option>)}
                </select>
              </div>
              <div className="row" style={{ marginTop: 12 }}>
                {type === "threshold" && <select value={op} onChange={(e) => setOp(e.target.value)} style={{ width: 70 }}><option>{">"}</option><option>{"<"}</option></select>}
                <input type="number" value={value} onChange={(e) => setValue(+e.target.value)} style={{ flex: 1 }} />
              </div>
            </>
          )}
          <label className="row" style={{ fontWeight: 400, marginTop: 12 }}>
            <input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} style={{ width: "auto" }} /> Email me when it fires
          </label>
          <button style={{ marginTop: 14 }} onClick={create}>Create alert</button>
          <p className="muted" style={{ fontSize: 12 }}>An alert fires once when its condition starts to hold and re-arms after it stops; unusual-move alerts fire once per observation.</p>
        </Card>
        <Card title="Active alerts">
          <ul className="list-tight">
            {alerts.filter((a) => a.status === "active").map((a) => (
              <li key={a.alert_id}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <strong>{a.name}</strong>
                  <button className="secondary" onClick={() => api(`/api/v1/alerts/${a.alert_id}`, { method: "DELETE" }).then(load)}>Remove</button>
                </div>
                <div className="muted mono">{describe(a.rule)}</div>
                <div className="muted" style={{ fontSize: 12 }}>Last evaluated {a.last_evaluated_at ?? "never"} · last triggered {a.last_triggered_at ?? "never"}</div>
                {(a.mine || a.notify_email) && (
                  <label className="row" style={{ fontWeight: 400, fontSize: 12.5, margin: "4px 0" }}>
                    <input type="checkbox" checked={a.notify_email} style={{ width: "auto" }}
                      onChange={(e) => api(`/api/v1/alerts/${a.alert_id}`, { method: "PATCH", body: JSON.stringify({ notify_email: e.target.checked }) }).then(load).catch(setError)} />
                    {a.mine ? "Email me when it fires" : "Emails its creator (you can turn this off)"}
                  </label>
                )}
                {a.events.slice(0, 3).map((e, i) => <div key={i} style={{ fontSize: 12.5 }}>⚑ {e.message}</div>)}
              </li>
            ))}
            {alerts.length === 0 && <li className="muted">No alerts yet.</li>}
          </ul>
        </Card>
      </div>
      </>}
    </>
  );
}
