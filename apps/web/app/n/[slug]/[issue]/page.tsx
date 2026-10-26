"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Issue = { newsletter: string; slug: string; subject: string; sent_at: string; html: string };

export default function PublicIssue() {
  const { slug, issue } = useParams<{ slug: string; issue: string }>();
  const [data, setData] = useState<Issue | null>(null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    api<Issue>(`/api/v1/public/newsletters/${encodeURIComponent(slug)}/issues/${encodeURIComponent(issue)}`).then(setData).catch(() => setMissing(true));
  }, [slug, issue]);

  if (missing) return <div className="public-page"><p className="muted">This issue is not available.</p></div>;
  if (!data) return null;
  return (
    <div className="public-page">
      <div className="muted" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: ".04em" }}><Link href={`/n/${data.slug}`}>{data.newsletter}</Link> · {data.sent_at.slice(0, 10)}</div>
      <h1 style={{ marginTop: 6 }}>{data.subject}</h1>
      {/* Server-rendered from Markdown with raw HTML escaped and only http(s)/mailto links kept. */}
      <article className="prose" dangerouslySetInnerHTML={{ __html: data.html }} />
      <div className="card" style={{ marginTop: 24 }}><Link href={`/n/${data.slug}`}>Subscribe to {data.newsletter}</Link></div>
    </div>
  );
}
