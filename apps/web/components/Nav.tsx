"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const ITEMS = [
  { href: "/", label: "Overview", hint: "Global market dashboard" },
  { href: "/research", label: "AI Research", hint: "Ask financial questions" },
  { href: "/markets", label: "Markets", hint: "Explore asset classes" },
  { href: "/economy", label: "Global Economy", hint: "Countries and macro data" },
  { href: "/quant", label: "Quant Lab", hint: "Models and analysis" },
  { href: "/reports", label: "Reports", hint: "Research outputs" },
  { href: "/intelligence", label: "Intelligence", hint: "Alerts and monitoring" },
  { href: "/data", label: "Data Explorer", hint: "Datasets and pipelines" },
];

export default function Nav() {
  const path = usePathname();
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
                <span>{it.label}</span>
                <small>{it.hint}</small>
              </Link>
            </li>
          );
        })}
      </ul>
      <p className="nav-foot">Research and decision support only. Not investment advice.</p>
    </nav>
  );
}
