"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api, type Account } from "@/lib/api";

const ITEMS = [
  { href: "/", label: "Overview", hint: "Global market dashboard" },
  { href: "/research", label: "AI Research", hint: "Ask financial questions" },
  { href: "/markets", label: "Markets", hint: "Explore asset classes" },
  { href: "/economy", label: "Global Economy", hint: "Countries and macro data" },
  { href: "/quant", label: "Quant Lab", hint: "Models and analysis" },
  { href: "/portfolio", label: "Portfolio", hint: "Risk, VaR and stress tests" },
  { href: "/reports", label: "Reports", hint: "Research outputs" },
  { href: "/intelligence", label: "Intelligence", hint: "Alerts and monitoring" },
  { href: "/data", label: "Data Explorer", hint: "Datasets and pipelines" },
];

export default function Nav() {
  const path = usePathname();
  const [account, setAccount] = useState<Account | null>(null);
  const [unread, setUnread] = useState(0);
  useEffect(() => {
    if (path === "/login" || path === "/reset") return;
    api<Account>("/api/v1/account").then(setAccount).catch(() => {});
    api<{ unread: number }>("/api/v1/agents").then((r) => setUnread(path.startsWith("/intelligence") ? 0 : r.unread)).catch(() => {});
  }, [path]);
  if (path === "/login" || path === "/reset") return null;
  return (
    <nav className="nav">
      <div className="brand">
        <span className="brand-mark">◆</span> ATLAS
      </div>
      <ul>
        {ITEMS.map((it) => {
          const active = it.href === "/" ? path === "/" : path.startsWith(it.href);
          return (
            <li key={it.href}>
              <Link href={it.href} className={active ? "active" : ""}>
                <span>{it.label}{it.href === "/intelligence" && unread > 0 && <span className="badge badge-delayed" style={{ marginLeft: 6 }}>{unread}</span>}</span>
                <small>{it.hint}</small>
              </Link>
            </li>
          );
        })}
      </ul>
      {account && (
        <Link href="/account" className={`nav-account ${path.startsWith("/account") ? "active" : ""}`}>
          <span>{account.user.name || account.user.email}</span>
          <small>{account.workspace?.name} · {account.workspace?.plan.replace("_", " ")}</small>
        </Link>
      )}
      <p className="nav-foot">Research and decision support only. Not investment advice.</p>
    </nav>
  );
}
