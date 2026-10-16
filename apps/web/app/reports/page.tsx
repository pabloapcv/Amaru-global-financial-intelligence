"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { API_URL, api } from "@/lib/api";
import { Card, ErrorNote, PageHeader } from "@/components/ui";

type ReportRow = { report_id: string; run_id: string; title: string; query: string; version: number; formats: string[]; created_at: string };

export default function Reports() {
  const [rows, setRows] = useState<ReportRow[]>([]);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api<ReportRow[]>("/api/v1/reports").then(setRows).catch(setError);
  }, []);

  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="Versioned research outputs. HTML includes a print stylesheet for PDF; Excel carries the evidence, sources and chart data."
        actions={<Link href="/research"><button>New research</button></Link>}
      />
      <ErrorNote error={error} />
      <Card>
        <table>
          <thead><tr><th>Report</th><th>Version</th><th>Created</th><th className="num">Downloads</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.report_id}>
                <td>
                  <Link href={`/research?run=${r.run_id}`}>{r.title}</Link>
                  <div className="muted" style={{ fontSize: 12 }}>{r.query}</div>
                </td>
                <td>v{r.version}</td>
                <td className="muted">{r.created_at.replace("T", " ").slice(0, 16)}</td>
                <td className="num">
                  <div className="row" style={{ justifyContent: "flex-end" }}>
                    {r.formats.includes("html") && <a href={`${API_URL}/api/v1/reports/${r.report_id}/download?format=html&inline=true`} target="_blank">HTML / PDF</a>}
                    {r.formats.includes("xlsx") && <a href={`${API_URL}/api/v1/reports/${r.report_id}/download?format=xlsx`}>Excel</a>}
                  </div>
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={4} className="muted">No reports yet.</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  );
}
