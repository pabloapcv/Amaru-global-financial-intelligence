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
  { href: "/scenarios", label: "Scenario Agent", hint: "What the data points to next" },
  { href: "/portfolio", label: "Portfolio", hint: "Risk, VaR and stress tests" },
  { href: "/reports", label: "Reports", hint: "Research outputs" },
  { href: "/intelligence", label: "Intelligence", hint: "Alerts and monitoring" },
  { href: "/data", label: "Data Explorer", hint: "Datasets and pipelines" },
];

const HIDDEN = ["/login", "/reset", "/verify", "/join", "/welcome"];
const hidden = (path: string) => HIDDEN.includes(path) || path.startsWith("/n/");  // public newsletter pages

export default function Nav() {
  const path = usePathname();
  const [account, setAccount] = useState<Account | null>(null);
  const [unread, setUnread] = useState(0);
  useEffect(() => {
    if (hidden(path)) return;
    api<Account>("/api/v1/account").then(setAccount).catch(() => {});
    api<{ unread: number }>("/api/v1/agents").then((r) => setUnread(path.startsWith("/intelligence") ? 0 : r.unread)).catch(() => {});
  }, [path]);
  if (hidden(path)) return null;
  const switchTo = async (workspaceId: string) => {
    await api("/api/v1/auth/switch-workspace", { method: "POST", body: JSON.stringify({ workspace_id: workspaceId }) });
    window.location.reload();  // every page reloads its data for the new workspace
  };
  return (
    <nav className="nav">
      <div className="brand">
        <span className="brand-mark">◆</span> Amaru
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
        {account && (account.role === "owner" || account.role === "admin") && account.auth !== "local" && (
          <li><Link href="/newsletter" className={path.startsWith("/newsletter") ? "active" : ""}><span>Newsletter</span><small>Publish research by email</small></Link></li>
        )}
        {account?.operator && (
          <li><Link href="/admin" className={path.startsWith("/admin") ? "active" : ""}><span>Operator console</span><small>All workspaces</small></Link></li>
        )}
        {account && <li className="nav-account-mobile"><Link href="/account" className={path.startsWith("/account") ? "active" : ""}><span>Account</span></Link></li>}
      </ul>
      {account && (
        <Link href="/account" className={`nav-account ${path.startsWith("/account") ? "active" : ""}`}>
          <span>{account.user.name || account.user.email}</span>
          <small>{account.workspace?.name}</small>
        </Link>
      )}
      {account?.workspaces && account.workspaces.length > 1 && (
        <select className="nav-workspace" value={account.workspace?.workspace_id} onChange={(e) => switchTo(e.target.value)} aria-label="Switch workspace">
          {account.workspaces.map((w) => <option key={w.workspace_id} value={w.workspace_id}>{w.name} ({w.role})</option>)}
        </select>
      )}
      <p className="nav-foot">Research and decision support only. Not investment advice.</p>
    </nav>
  );
}
