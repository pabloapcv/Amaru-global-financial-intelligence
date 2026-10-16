"use client";

import { useEffect, useRef } from "react";
import { createChart, ColorType, LineStyle, type IChartApi } from "lightweight-charts";
import type { ChartSpec } from "@/lib/api";

const PALETTE = ["#4c8dff", "#f08a4b", "#2ec4b6", "#d65db1", "#9aa5b1", "#e9c46a"];

function cssVar(name: string, fallback: string) {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

export default function Chart({ spec, height = 280 }: { spec: ChartSpec; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart: IChartApi = createChart(ref.current, {
      height,
      width: ref.current.clientWidth,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: cssVar("--muted", "#8a94a3"),
        fontFamily: "inherit",
      },
      grid: {
        vertLines: { color: cssVar("--grid", "#1f2733") },
        horzLines: { color: cssVar("--grid", "#1f2733") },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, minBarSpacing: 0.01 },
      crosshair: { mode: 1 },
      localization: {
        // Compact labels for large magnitudes (trade values, contracts); plain decimals otherwise.
        priceFormatter: (v: number) =>
          Math.abs(v) >= 1e4
            ? new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(v)
            : v.toFixed(Math.abs(v) >= 100 ? 1 : 2),
      },
    });
    const clean = (pts: { date: string; value: number | null }[]) => {
      const seen = new Set<string>();
      return pts
        .filter((p) => p.value !== null && !seen.has(p.date) && seen.add(p.date))
        .map((p) => ({ time: p.date, value: p.value as number }))
        .sort((a, b) => (a.time < b.time ? -1 : 1));
    };
    if (spec.x_is_event_time) {
      // Map anchor-relative business days back to "day N" labels.
      const anchor = Date.UTC(2000, 0, 3);
      const dayOf = (t: unknown) => {
        const ms = typeof t === "number" ? t * 1000 : typeof t === "string" ? Date.parse(t) : Date.UTC((t as any).year, (t as any).month - 1, (t as any).day);
        const cal = Math.round((ms - anchor) / 86400000);
        const weeks = Math.floor(cal / 7);
        return weeks * 5 + Math.min(cal - weeks * 7, 5);
      };
      chart.applyOptions({
        timeScale: { tickMarkFormatter: (t: unknown) => `day ${dayOf(t)}` },
        localization: { timeFormatter: (t: unknown) => `day ${dayOf(t)}` },
      });
    }
    spec.series.forEach((s, i) => {
      let pts = clean(s.points);
      if (spec.type === "forecast" && s.name === "forecast" && spec.series[0]?.points.length) {
        const last = spec.series[0].points[spec.series[0].points.length - 1];
        pts = clean([last, ...s.points]);
      }
      const line = chart.addLineSeries({
        color: PALETTE[i % PALETTE.length],
        lineWidth: 2,
        lineStyle: s.name === "forecast" ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false,
        title: spec.series.length > 1 ? s.name : "",
      });
      line.setData(pts);
    });
    if (spec.band?.length) {
      for (const key of ["low", "high"] as const) {
        const b = chart.addLineSeries({ color: PALETTE[0], lineWidth: 1, lineStyle: LineStyle.Dotted, priceLineVisible: false, title: key === "low" ? "p10" : "p90" });
        b.setData(clean(spec.band.map((p) => ({ date: p.date, value: p[key] }))));
      }
    }
    chart.timeScale().fitContent();
    // Re-fit when the container is laid out or resized so the full range stays visible.
    const observer = new ResizeObserver(([entry]) => {
      chart.applyOptions({ width: Math.floor(entry.contentRect.width) });
      chart.timeScale().fitContent();
    });
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [spec, height]);

  return <div ref={ref} style={{ width: "100%", height }} />;
}
