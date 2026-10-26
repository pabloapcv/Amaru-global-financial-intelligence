"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ErrorNote } from "@/components/ui";

type Info = { slug: string; name: string; description: string; public_archive: boolean; issues: { issue_id: string; subject: string; sent_at: string }[] };

export default function PublicNewsletter() {
  const { slug } = useParams<{ slug: string }>();
  const [info, setInfo] = useState<Info | null>(null);
  const [missing, setMissing] = useState(false);
  const [email, setEmail] = useState("");
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => { api<Info>(`/api/v1/public/newsletters/${encodeURIComponent(slug)}`).then(setInfo).catch(() => setMissing(true)); }, [slug]);

  const subscribe = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { await api(`/api/v1/public/newsletters/${encodeURIComponent(slug)}/subscribe`, { method: "POST", body: JSON.stringify({ email }) }); setDone(true); }
    catch (err) { setError(err); } finally { setBusy(false); }
  };

  if (missing) return <div className="public-page"><p className="muted">This newsletter does not exist.</p></div>;
  if (!info) return null;
  return (
    <div className="public-page">
      <h1 style={{ marginBottom: 6 }}>{info.name}</h1>
      {info.description && <p style={{ whiteSpace: "pre-wrap" }}>{info.description}</p>}
      <div className="card" style={{ margin: "18px 0" }}>
        {done ? <p style={{ margin: 0 }}>Check your inbox: we sent a link to confirm your subscription.</p> : (
          <form onSubmit={subscribe} className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" aria-label="Your email" style={{ flex: "1 1 220px" }} />
            <button type="submit" disabled={busy}>Subscribe</button>
          </form>
        )}
        <ErrorNote error={error} />
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>Free. Unsubscribe with one click from any email. General research based on public data, not personalised investment advice.</p>
      </div>
      {info.issues.length > 0 && (
        <>
          <h2 style={{ fontSize: 16 }}>Past issues</h2>
          <ul style={{ paddingLeft: 18 }}>
            {info.issues.map((i) => <li key={i.issue_id}><Link href={`/n/${info.slug}/${i.issue_id}`}>{i.subject}</Link> <span className="muted">· {i.sent_at.slice(0, 10)}</span></li>)}
          </ul>
        </>
      )}
      <p className="muted" style={{ fontSize: 12, marginTop: 28 }}>Published with ATLAS</p>
    </div>
  );
}
