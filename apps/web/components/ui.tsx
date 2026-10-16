import type { Freshness } from "@/lib/api";
import type { ReactNode } from "react";

const LABEL: Record<Freshness, string> = {
  current: "Current",
  delayed: "Delayed",
  stale: "Stale",
  no_data: "No data",
  no_source: "No source",
  failing: "Failing",
};

export function FreshnessBadge({ value, title }: { value: Freshness; title?: string }) {
  return (
    <span className={`badge badge-${value}`} title={title}>
      {LABEL[value] ?? value}
    </span>
  );
}

export function Card({ title, children, actions }: { title?: ReactNode; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <h3>{title}</h3>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="note note-error">
      {msg.includes("Failed to fetch") ? "Cannot reach the ATLAS API. Start it with `atlas serve`." : msg}
    </div>
  );
}

export function Change({ value, unit = "%" }: { value: number | null | undefined; unit?: string }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  const cls = value > 0 ? "up" : value < 0 ? "down" : "";
  return (
    <span className={cls}>
      {value > 0 ? "+" : ""}
      {value.toFixed(unit === "bp" ? 1 : 2)}
      {unit === "bp" ? " bp" : unit}
    </span>
  );
}
