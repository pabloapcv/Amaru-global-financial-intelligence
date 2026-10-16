"use client";

import { useEffect, useState } from "react";
import Chart from "@/components/Chart";
import { api, fmt, type ChartSpec } from "@/lib/api";
import { Card } from "@/components/ui";

type PositioningResult = {
  name: string;
  report_date: string;
  noncomm_net: number;
  noncomm_net_change_1w: number;
  noncomm_net_change_4w: number | null;
  noncomm_net_pct_oi: number;
  comm_net: number;
  open_interest: number;
  cot_index: number | null;
  net_zscore: number | null;
  positioning_extreme: string | null;
  description: string;
  freshness: string;
};

function CotGauge({ value }: { value: number | null }) {
  if (value === null) return <span className="muted">—</span>;
  const tone = value >= 90 || value <= 10 ? "var(--warn)" : "var(--accent)";
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <div style={{ position: "relative", height: 8, borderRadius: 4, background: "var(--panel-2)", border: "1px solid var(--line)" }}>
        <div style={{ position: "absolute", left: `calc(${value}% - 5px)`, top: -3, width: 10, height: 12, borderRadius: 3, background: tone }} />
      </div>
      <div className="row muted" style={{ justifyContent: "space-between", fontSize: 11 }}>
        <span>most short</span><span>{fmt(value, 1)}</span><span>most long</span>
      </div>
    </div>
  );
}

export default function Positioning({ id }: { id: string }) {
  const [data, setData] = useState<{ result: PositioningResult; chart: ChartSpec; citation: string } | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    setData(null);
    setMissing(false);
    api<{ result: PositioningResult; chart: ChartSpec; citation: string }>(`/api/v1/positioning?id=${id}&lookback_years=3`)
      .then(setData)
      .catch(() => setMissing(true));
  }, [id]);

  if (missing) return null;
  if (!data) return <Card title="Positioning"><span className="muted">Loading…</span></Card>;
  const r = data.result;
  return (
    <Card title={`Positioning · CFTC COT · ${r.report_date}`}>
      <div className="grid grid-2" style={{ marginBottom: 10 }}>
        <dl className="kv">
          <dt>Speculators net</dt><dd>{fmt(r.noncomm_net, 0)} ({fmt(r.noncomm_net_pct_oi, 1)}% of OI)</dd>
          <dt>1w / 4w change</dt><dd>{fmt(r.noncomm_net_change_1w, 0)} / {fmt(r.noncomm_net_change_4w, 0)}</dd>
          <dt>Hedgers net</dt><dd>{fmt(r.comm_net, 0)}</dd>
          <dt>Open interest</dt><dd>{fmt(r.open_interest, 0)}</dd>
          <dt>Z-score (3y)</dt><dd>{fmt(r.net_zscore)}</dd>
        </dl>
        <div>
          <label>COT index (3-year range)</label>
          <CotGauge value={r.cot_index} />
          {r.positioning_extreme && <div className="note note-warn" style={{ marginTop: 10 }}>Speculators look {r.positioning_extreme}. Crowding raises reversal risk; it is not a timing signal.</div>}
        </div>
      </div>
      <Chart spec={data.chart} height={220} />
      <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>{r.description}</p>
    </Card>
  );
}
