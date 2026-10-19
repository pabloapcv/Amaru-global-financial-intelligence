"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Step = { key: string; title: string; done: boolean; href: string; help: string };
const DISMISS_KEY = "atlas-get-started-dismissed";

export default function GetStarted() {
  const router = useRouter();
  const [steps, setSteps] = useState<Step[] | null>(null);
  const [hidden, setHidden] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let dismissed = false;
    try { dismissed = localStorage.getItem(DISMISS_KEY) === "1"; } catch {}
    if (dismissed) return;
    api<{ steps: Step[]; complete: boolean }>("/api/v1/onboarding")
      .then((r) => { setSteps(r.steps); setHidden(r.complete); })
      .catch(() => {});
  }, []);

  if (hidden || !steps) return null;
  const done = steps.filter((s) => s.done).length;
  const dismiss = () => { try { localStorage.setItem(DISMISS_KEY, "1"); } catch {} setHidden(true); };
  const examplePortfolio = async () => {
    setBusy(true);
    try { await api("/api/v1/onboarding/example-portfolio", { method: "POST" }); router.push("/portfolio"); } finally { setBusy(false); }
  };

  return (
    <section className="card" style={{ marginBottom: 18 }} aria-labelledby="gs-title">
      <header className="card-head">
        <h3 id="gs-title">Get started · {done} of {steps.length} done</h3>
        <button className="secondary" onClick={dismiss}>Hide</button>
      </header>
      <div style={{ height: 6, background: "var(--panel-2)", borderRadius: 3, marginBottom: 12 }}>
        <div style={{ width: `${(done / steps.length) * 100}%`, height: 6, borderRadius: 3, background: "var(--accent)" }} />
      </div>
      <ol className="list-tight" style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {steps.map((s) => (
          <li key={s.key} className="row" style={{ justifyContent: "space-between", flexWrap: "nowrap", gap: 12 }}>
            <div className="row" style={{ flexWrap: "nowrap", alignItems: "flex-start" }}>
              <span aria-hidden="true" className={s.done ? "up" : "muted"} style={{ width: 18 }}>{s.done ? "✓" : "○"}</span>
              <div>
                <div style={{ fontWeight: 600, textDecoration: s.done ? "line-through" : "none", opacity: s.done ? 0.6 : 1 }}>{s.title}</div>
                <div className="muted" style={{ fontSize: 12.5 }}>{s.help}</div>
              </div>
            </div>
            {!s.done && (
              s.key === "portfolio"
                ? <button className="secondary" onClick={examplePortfolio} disabled={busy}>{busy ? "Creating…" : "Try an example"}</button>
                : <Link href={s.href}><button className="secondary">Open</button></Link>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
