"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import InstrumentDetail from "@/components/InstrumentDetail";
import { api, type Instrument } from "@/lib/api";
import { Card, ErrorNote, FreshnessBadge, PageHeader } from "@/components/ui";

const CLASSES = ["commodity", "fx", "rates", "equity", "volatility"];
function Markets() {
  const params = useSearchParams();
  const router = useRouter();
  const [instruments, setInstruments] = useState<Instrument[]>([]);
  const [cls, setCls] = useState("commodity");
  const [error, setError] = useState<unknown>(null);
  const selected = params.get("id");

  useEffect(() => {
    api<Instrument[]>("/api/v1/instruments").then(setInstruments).catch(setError);
  }, []);
  useEffect(() => {
    const inst = instruments.find((i) => i.dataset_id === selected);
    if (inst) setCls(inst.asset_class);
  }, [selected, instruments]);

  const list = useMemo(() => instruments.filter((i) => i.kind === "market" && i.asset_class === cls), [instruments, cls]);
  const current = instruments.find((i) => i.dataset_id === selected);

  return (
    <>
      <PageHeader title="Markets" subtitle="Canonical instruments across commodities, currencies, rates and equities." />
      <ErrorNote error={error} />
      <div className="split">
        <Card title={<div className="seg">{CLASSES.map((c) => <button key={c} className={c === cls ? "on" : ""} onClick={() => setCls(c)}>{c === "fx" ? "FX" : c[0].toUpperCase() + c.slice(1)}</button>)}</div>}>
          <table>
            <tbody>
              {list.map((i) => (
                <tr key={i.dataset_id} className={`clickable ${i.dataset_id === selected ? "selected" : ""}`} onClick={() => router.push(`/markets?id=${i.dataset_id}`)}>
                  <td>
                    <div>{i.name.split(" (")[0]}</div>
                    <div className="muted mono">{i.dataset_id}</div>
                  </td>
                  <td className="num"><FreshnessBadge value={i.freshness} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <div>
          {current && !current.has_source ? (
            <Card title={current.name}><div className="note note-warn">{current.coverage_note}</div><p className="muted">{current.description}</p></Card>
          ) : selected ? (
            <InstrumentDetail id={selected} />
          ) : (
            <Card><span className="muted">Select an instrument.</span></Card>
          )}
          {current?.description && current.has_source && <p className="muted" style={{ fontSize: 12.5 }}>{current.description}</p>}
        </div>
      </div>
    </>
  );
}

export default function MarketsPage() {
  return <Suspense><Markets /></Suspense>;
}
