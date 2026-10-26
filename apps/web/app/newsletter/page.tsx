"use client";

import { useCallback, useEffect, useState } from "react";
import { API_URL, api } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type Settings = { slug: string; name: string; description: string; from_name: string; reply_to: string | null; postal_address: string;
  public_archive: boolean; language: "en" | "es" | "pt"; public_url?: string; subscribers?: { active: number; pending: number; unsubscribed: number }; subscriber_limit?: number };
type Subscriber = { subscriber_id: string; email: string; status: string; source: string; created_at: string };
type Issue = { issue_id: string; run_id: string | null; subject: string; intro: string; status: string; created_at: string; sent_at: string | null; recipients: number };
type Run = { run_id: string; query: string; status: string; title: string | null; created_at: string };

const EMPTY: Settings = { slug: "", name: "", description: "", from_name: "", reply_to: "", postal_address: "", public_archive: true, language: "en" };

export default function NewsletterPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [form, setForm] = useState<Settings>(EMPTY);
  const [emailOn, setEmailOn] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [subs, setSubs] = useState<Subscriber[]>([]);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runId, setRunId] = useState("");
  const [invite, setInvite] = useState("");
  const [draft, setDraft] = useState<(Issue & { html?: string }) | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    try {
      const r = await api<{ newsletter: Settings | null; email_configured: boolean }>("/api/v1/newsletter");
      setSettings(r.newsletter); setEmailOn(r.email_configured);
      if (r.newsletter) {
        setForm({ ...r.newsletter, reply_to: r.newsletter.reply_to ?? "" });
        const [s, i, runList] = await Promise.all([api<Subscriber[]>("/api/v1/newsletter/subscribers"), api<Issue[]>("/api/v1/newsletter/issues"),
          api<Run[]>("/api/v1/research?limit=50")]);
        setSubs(s); setIssues(i); setRuns(runList.filter((x) => x.status === "complete"));
      }
    } catch (e) { setError(e); } finally { setLoaded(true); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const run = async (fn: () => Promise<void>) => { setError(null); setNote(null); try { await fn(); await load(); } catch (e) { setError(e); } };
  const save = (e: React.FormEvent) => { e.preventDefault(); run(async () => { await api("/api/v1/newsletter", { method: "PUT", body: JSON.stringify(form) }); setNote("Settings saved."); }); };
  const openDraft = (id: string) => run(async () => setDraft(await api(`/api/v1/newsletter/issues/${id}`)));
  const createIssue = () => run(async () => { const i = await api<Issue>("/api/v1/newsletter/issues", { method: "POST", body: JSON.stringify({ run_id: runId }) }); setDraft(await api(`/api/v1/newsletter/issues/${i.issue_id}`)); setRunId(""); });
  const saveDraft = () => draft && run(async () => {
    await api(`/api/v1/newsletter/issues/${draft.issue_id}`, { method: "PATCH", body: JSON.stringify({ subject: draft.subject, intro: draft.intro }) });
    setDraft(await api(`/api/v1/newsletter/issues/${draft.issue_id}`)); setNote("Draft saved.");
  });
  const testDraft = () => draft && run(async () => { await saveDraftQuiet(); const r = await api<{ to: string }>(`/api/v1/newsletter/issues/${draft.issue_id}/test`, { method: "POST" }); setNote(`Test sent to ${r.to}.`); });
  const saveDraftQuiet = async () => { if (draft) await api(`/api/v1/newsletter/issues/${draft.issue_id}`, { method: "PATCH", body: JSON.stringify({ subject: draft.subject, intro: draft.intro }) }); };
  const sendDraft = () => draft && settings && window.confirm(`Send “${draft.subject}” to ${settings.subscribers?.active ?? 0} confirmed subscriber(s)? This cannot be undone.`) && run(async () => {
    await saveDraftQuiet();
    const r = await api<{ subscribers: number }>(`/api/v1/newsletter/issues/${draft.issue_id}/send`, { method: "POST" });
    setDraft(null); setNote(`Sending to ${r.subscribers} subscriber(s).`);
  });
  const deleteDraft = (id: string) => window.confirm("Delete this draft?") && run(async () => { await api(`/api/v1/newsletter/issues/${id}`, { method: "DELETE" }); if (draft?.issue_id === id) setDraft(null); });
  const inviteOne = (e: React.FormEvent) => { e.preventDefault(); run(async () => { await api("/api/v1/newsletter/subscribers", { method: "POST", body: JSON.stringify({ email: invite }) }); setNote(`Confirmation email sent to ${invite}.`); setInvite(""); }); };
  const removeSub = (s: Subscriber) => window.confirm(`Erase ${s.email} from the list?`) && run(async () => { await api(`/api/v1/newsletter/subscribers/${s.subscriber_id}`, { method: "DELETE" }); });
  const field = (k: keyof Settings) => ({ value: String(form[k] ?? ""), onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setForm({ ...form, [k]: e.target.value }) });

  if (!loaded) return null;
  return (
    <>
      <PageHeader title="Newsletter" subtitle="Publish your verified research by email. Readers subscribe on your public page and confirm their address; every email carries an unsubscribe link and your postal address." />
      <ErrorNote error={error} />
      {note && <div className="note" style={{ marginBottom: 14 }}>{note}</div>}
      {!emailOn && <div className="note note-warn" style={{ marginBottom: 14 }}>Email is not configured on this server (ATLAS_SMTP_*): messages are written to the API log instead of being sent.</div>}

      {settings && (
        <div className="grid grid-4" style={{ marginBottom: 14 }}>
          <Card title="Confirmed"><div className="quote"><div className="value">{settings.subscribers?.active ?? 0}</div><div className="sub">of {settings.subscriber_limit?.toLocaleString()} on your plan</div></div></Card>
          <Card title="Awaiting confirmation"><div className="quote"><div className="value">{settings.subscribers?.pending ?? 0}</div><div className="sub">links valid 7 days</div></div></Card>
          <Card title="Unsubscribed"><div className="quote"><div className="value">{settings.subscribers?.unsubscribed ?? 0}</div><div className="sub">never emailed again</div></div></Card>
          <Card title="Issues sent"><div className="quote"><div className="value">{issues.filter((i) => i.status === "sent").length}</div><div className="sub"><a href={settings.public_url} target="_blank" rel="noreferrer">Public page</a></div></div></Card>
        </div>
      )}

      {settings && (
        <Card title="Issues">
          <div className="row" style={{ flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
            <select value={runId} onChange={(e) => setRunId(e.target.value)} aria-label="Research run" style={{ flex: "1 1 320px" }}>
              <option value="">Choose completed research to turn into an issue…</option>
              {runs.map((r) => <option key={r.run_id} value={r.run_id}>{(r.title || r.query).slice(0, 90)} · {r.created_at.slice(0, 10)}</option>)}
            </select>
            <button onClick={createIssue} disabled={!runId}>New draft</button>
          </div>
          {issues.length === 0 ? <p className="muted">No issues yet.</p> : (
            <div style={{ overflowX: "auto" }}>
              <table>
                <thead><tr><th>Subject</th><th>Status</th><th className="num">Recipients</th><th className="num"></th></tr></thead>
                <tbody>{issues.map((i) => (
                  <tr key={i.issue_id}>
                    <td>{i.subject}<div className="muted" style={{ fontSize: 11.5 }}>{i.sent_at ? `sent ${i.sent_at.slice(0, 16).replace("T", " ")} UTC` : `drafted ${i.created_at.slice(0, 10)}`}</div></td>
                    <td>{i.status}</td>
                    <td className="num">{i.recipients}</td>
                    <td className="num" style={{ whiteSpace: "nowrap" }}>
                      {i.status === "draft"
                        ? <><button className="secondary" onClick={() => openDraft(i.issue_id)}>Edit</button>{" "}<button className="secondary" onClick={() => deleteDraft(i.issue_id)}>Delete</button></>
                        : settings.public_archive && i.status === "sent" && <a href={`/n/${settings.slug}/${i.issue_id}`} target="_blank" rel="noreferrer">View</a>}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {draft && (
            <div style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 14 }}>
              <label>Subject<input value={draft.subject} onChange={(e) => setDraft({ ...draft, subject: e.target.value })} maxLength={200} /></label>
              <label>Introduction (optional, above the research)<textarea rows={4} value={draft.intro} onChange={(e) => setDraft({ ...draft, intro: e.target.value })} maxLength={5000} /></label>
              <div className="row" style={{ gap: 8, margin: "8px 0 12px", flexWrap: "wrap" }}>
                <button className="secondary" onClick={saveDraft}>Save and preview</button>
                <button className="secondary" onClick={testDraft}>Send me a test</button>
                <button onClick={sendDraft}>Send to subscribers</button>
                <button className="secondary" onClick={() => setDraft(null)}>Close</button>
              </div>
              {draft.html && (
                <div className="card" style={{ maxHeight: 480, overflowY: "auto" }}>
                  <div className="muted" style={{ fontSize: 12 }}>{settings.name}</div>
                  <h2 style={{ marginTop: 4 }}>{draft.subject}</h2>
                  <div className="prose" dangerouslySetInnerHTML={{ __html: draft.html }} />
                </div>
              )}
            </div>
          )}
        </Card>
      )}

      <div className="grid grid-2" style={{ marginTop: 14 }}>
        <Card title={settings ? "Settings" : "Set up your newsletter"}>
          <form onSubmit={save}>
            <label>Name<input {...field("name")} required maxLength={120} placeholder="Sugar Weekly" /></label>
            <label>Public address<div className="row" style={{ flexWrap: "nowrap", gap: 4 }}><span className="muted" style={{ whiteSpace: "nowrap" }}>/n/</span><input {...field("slug")} required maxLength={40} placeholder="sugar-weekly" /></div></label>
            <label>Language (public page, emails and scheduled research)
              <select value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value as Settings["language"] })}>
                <option value="en">English</option><option value="es">Español</option><option value="pt">Português</option>
              </select>
            </label>
            <label>Description<textarea {...field("description")} rows={3} maxLength={2000} placeholder="What readers get, and how often" /></label>
            <label>Sender name<input {...field("from_name")} maxLength={120} placeholder="Defaults to the newsletter name" /></label>
            <label>Reply-to email<input {...field("reply_to")} type="email" maxLength={320} /></label>
            <label>Postal address (required to send; shown in every email)<input {...field("postal_address")} maxLength={300} /></label>
            <label className="row" style={{ fontWeight: 400 }}><input type="checkbox" checked={form.public_archive} onChange={(e) => setForm({ ...form, public_archive: e.target.checked })} style={{ width: "auto" }} />Publish sent issues on the public page</label>
            <button type="submit">{settings ? "Save settings" : "Create newsletter"}</button>
          </form>
        </Card>
        {settings && (
          <Card title="Subscribers" actions={<a href={`${API_URL}/api/v1/newsletter/subscribers?format=csv`}>Export CSV</a>}>
            <form onSubmit={inviteOne} className="row" style={{ gap: 8, marginBottom: 10, flexWrap: "nowrap" }}>
              <input type="email" value={invite} onChange={(e) => setInvite(e.target.value)} placeholder="Invite by email (they confirm)" aria-label="Invite subscriber" required />
              <button type="submit" className="secondary">Invite</button>
            </form>
            <div style={{ overflowX: "auto", maxHeight: 420, overflowY: "auto" }}>
              <table>
                <thead><tr><th>Email</th><th>Status</th><th>Since</th><th className="num"></th></tr></thead>
                <tbody>{subs.map((s) => (
                  <tr key={s.subscriber_id}><td style={{ overflowWrap: "anywhere" }}>{s.email}</td><td>{s.status}</td><td className="muted">{s.created_at.slice(0, 10)}</td>
                    <td className="num"><button className="secondary" onClick={() => removeSub(s)} aria-label={`Erase ${s.email}`}>Erase</button></td></tr>
                ))}</tbody>
              </table>
            </div>
          </Card>
        )}
      </div>
    </>
  );
}
