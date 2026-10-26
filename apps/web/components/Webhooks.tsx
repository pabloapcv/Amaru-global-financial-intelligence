"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, ErrorNote } from "@/components/ui";

type Hook = { webhook_id: string; url: string; description: string; events: string[]; format: "atlas" | "slack"; status: string;
  disabled_reason: string | null; consecutive_failures: number; last_delivery_at: string | null };
type Delivery = { delivery_id: string; event: string; status: string; attempts: number; response_code: number | null; error: string | null; created_at: string };

export default function Webhooks() {
  const [hooks, setHooks] = useState<Hook[] | null>(null);
  const [events, setEvents] = useState<Record<string, string>>({});
  const [url, setUrl] = useState("");
  const [format, setFormat] = useState<"atlas" | "slack">("atlas");
  const [chosen, setChosen] = useState<string[]>(["alert.triggered", "agent.fired"]);
  const [secret, setSecret] = useState<{ id: string; value: string } | null>(null);
  const [log, setLog] = useState<{ id: string; rows: Delivery[] } | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(() => {
    api<{ webhooks: Hook[]; events: Record<string, string> }>("/api/v1/webhooks")
      .then((r) => { setHooks(r.webhooks); setEvents(r.events); }).catch(setError);
  }, []);
  useEffect(() => { load(); }, [load]);
  if (hooks === null) return error ? <ErrorNote error={error} /> : null;

  const run = async (fn: () => Promise<void>) => { setError(null); setNote(null); try { await fn(); load(); } catch (e) { setError(e); } };
  const add = () => run(async () => {
    const r = await api<Hook & { secret: string }>("/api/v1/webhooks", { method: "POST", body: JSON.stringify({ url, events: chosen, format }) });
    setSecret({ id: r.webhook_id, value: r.secret }); setUrl("");
  });
  const patch = (id: string, body: object) => run(async () => { await api(`/api/v1/webhooks/${id}`, { method: "PATCH", body: JSON.stringify(body) }); });
  const remove = (h: Hook) => window.confirm(`Delete the webhook to ${h.url}?`) && run(async () => { await api(`/api/v1/webhooks/${h.webhook_id}`, { method: "DELETE" }); });
  const test = (id: string) => run(async () => {
    const r = await api<{ status: string; response_code: number | null; error: string | null }>(`/api/v1/webhooks/${id}/test`, { method: "POST" });
    setNote(r.status === "delivered" ? `Test delivered (HTTP ${r.response_code}).` : `Test failed: ${r.error}`);
  });
  const rotate = (id: string) => window.confirm("Rotate the signing secret? The old one stops working immediately.") && run(async () => {
    setSecret({ id, value: (await api<{ secret: string }>(`/api/v1/webhooks/${id}/rotate-secret`, { method: "POST" })).secret });
  });
  const showLog = (id: string) => run(async () => {
    setLog(log?.id === id ? null : { id, rows: await api<Delivery[]>(`/api/v1/webhooks/${id}/deliveries?limit=20`) });
  });
  const toggle = (e: string) => setChosen((c) => (c.includes(e) ? c.filter((x) => x !== e) : [...c, e]));

  return (
    <Card title="Webhooks">
      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
        Send alerts, agent firings and finished research to your own systems or a Slack channel. ATLAS deliveries are JSON signed with the webhook&apos;s secret (<code>X-Atlas-Signature</code>); failed deliveries are retried for about 9 hours.
      </p>
      {hooks.length > 0 && (
        <div style={{ overflowX: "auto", marginBottom: 12 }}>
          <table>
            <thead><tr><th>Endpoint</th><th>Events</th><th>Status</th><th className="num"></th></tr></thead>
            <tbody>
              {hooks.map((h) => (
                <tr key={h.webhook_id}>
                  <td style={{ overflowWrap: "anywhere" }}>{h.url}<div className="muted" style={{ fontSize: 11.5 }}>{h.format === "slack" ? "Slack message" : "Signed JSON"}{h.last_delivery_at ? ` · last delivery ${h.last_delivery_at.slice(0, 16).replace("T", " ")} UTC` : ""}</div></td>
                  <td className="muted" style={{ fontSize: 12 }}>{h.events.join(", ")}</td>
                  <td>{h.status}{h.disabled_reason && <div className="muted" style={{ fontSize: 11.5 }}>{h.disabled_reason}</div>}</td>
                  <td className="num" style={{ whiteSpace: "nowrap" }}>
                    <button className="secondary" onClick={() => test(h.webhook_id)}>Send test</button>{" "}
                    <button className="secondary" onClick={() => showLog(h.webhook_id)}>Deliveries</button>{" "}
                    {h.status === "active"
                      ? <button className="secondary" onClick={() => patch(h.webhook_id, { status: "paused" })}>Pause</button>
                      : <button className="secondary" onClick={() => patch(h.webhook_id, { status: "active" })}>Turn on</button>}{" "}
                    {h.format === "atlas" && <><button className="secondary" onClick={() => rotate(h.webhook_id)}>Rotate secret</button>{" "}</>}
                    <button className="secondary" onClick={() => remove(h)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {secret && (
        <div className="note" style={{ marginBottom: 12 }}>
          Signing secret (shown once; store it where your endpoint can read it): <code style={{ overflowWrap: "anywhere" }}>{secret.value}</code>{" "}
          <button className="secondary" onClick={() => setSecret(null)}>Done</button>
        </div>
      )}
      {log && (
        <div style={{ overflowX: "auto", marginBottom: 12 }}>
          {log.rows.length === 0 ? <p className="muted">No deliveries yet.</p> : (
            <table>
              <thead><tr><th>When (UTC)</th><th>Event</th><th>Result</th><th className="num">Attempts</th></tr></thead>
              <tbody>{log.rows.map((d) => (
                <tr key={d.delivery_id}><td>{d.created_at.slice(0, 16).replace("T", " ")}</td><td>{d.event}</td>
                  <td>{d.status}{d.error ? ` · ${d.error}` : d.response_code ? ` · HTTP ${d.response_code}` : ""}</td><td className="num">{d.attempts}</td></tr>
              ))}</tbody>
            </table>
          )}
        </div>
      )}
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://your-endpoint.example.com/atlas" aria-label="Webhook URL" style={{ flex: "1 1 260px" }} />
        <select value={format} onChange={(e) => setFormat(e.target.value as "atlas" | "slack")} aria-label="Format" style={{ width: "auto" }}>
          <option value="atlas">Signed JSON</option>
          <option value="slack">Slack incoming webhook</option>
        </select>
        <button onClick={add} disabled={!url || chosen.length === 0}>Add webhook</button>
      </div>
      <div className="row" style={{ flexWrap: "wrap", gap: 14, marginTop: 8 }}>
        {Object.entries(events).map(([e, label]) => (
          <label key={e} className="row" style={{ fontWeight: 400, margin: 0, fontSize: 12.5 }} title={label}>
            <input type="checkbox" checked={chosen.includes(e)} onChange={() => toggle(e)} style={{ width: "auto" }} />{e}
          </label>
        ))}
      </div>
      {note && <p className="muted" style={{ marginBottom: 0 }}>{note}</p>}
      <ErrorNote error={error} />
    </Card>
  );
}
