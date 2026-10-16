"use client";

import { useEffect, useState } from "react";
import InstrumentDetail from "@/components/InstrumentDetail";
import { api, type Instrument } from "@/lib/api";
import { Card, ErrorNote, FreshnessBadge, PageHeader } from "@/components/ui";

export default function Economy() {
  const [series, setSeries] = useState<Instrument[]>([]);
  const [selected, setSelected] = useState("US.CPI.HEADLINE");
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    api<Instrument[]>("/api/v1/instruments").then((all) => setSeries(all.filter((i) => i.kind === "macro"))).catch(setError);
  }, []);

  return (
    <>
      <PageHeader title="Global economy" subtitle="Point-in-time macro series: each revision is stored as a new vintage, so past views can be reproduced." />
      <ErrorNote error={error} />
      <div className="split">
        <Card title="United States">
          <table>
            <tbody>
              {series.map((s) => (
                <tr key={s.dataset_id} className={`clickable ${s.dataset_id === selected ? "selected" : ""}`} onClick={() => setSelected(s.dataset_id)}>
                  <td>{s.name}<div className="muted" style={{ fontSize: 11.5 }}>{s.frequency} · {s.unit}</div></td>
                  <td className="num"><FreshnessBadge value={s.freshness} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: 12 }}>
            Country coverage beyond the U.S. (World Bank, IMF, BIS connectors) is on the roadmap; comparisons stay limited to connected series until then.
          </p>
        </Card>
        <InstrumentDetail id={selected} defaultYears={10} />
      </div>
    </>
  );
}
