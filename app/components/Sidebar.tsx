"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Suspense } from "react";

type Item = { href: string; label: string; icon?: string; children?: Item[] };

const items: Item[] = [
  { href: "/", label: "Dashboard", icon: "▦" },
  {
    href: "/prices",
    label: "Load Prices",
    icon: "↓",
    children: [{ href: "/prices/sp500", label: "S&P 500 universe" }],
  },
  {
    href: "/analysis",
    label: "Analysis",
    icon: "📈",
    children: [{ href: "/analysis/single", label: "Single stock" }],
  },
  {
    href: "/agent",
    label: "AI agent",
    icon: "✦",
    children: [
      { href: "/agent/backtest?skill=warren-buffett", label: "Warren Buffett" },
      { href: "/agent/run?skill=jamie-dimon", label: "Jamie Dimon" },
      { href: "/agent/backtest?skill=john-murphy", label: "John Murphy" },
      { href: "/agent/scan", label: "S&P 500 scan" },
      { href: "/agent", label: "Skill library" },
    ],
  },
  {
    href: "/diagnosis",
    label: "Diagnosis",
    icon: "⚙",
    children: [{ href: "/diagnosis/runs", label: "Run history" }],
  },
];

// A child link is active when its path matches and, if it carries a query (e.g. ?skill=x),
// that query matches too - so two skills under the same page highlight independently.
function isActive(pathname: string, search: URLSearchParams, href: string, parentHref: string) {
  const [path, query] = href.split("?");
  if (path !== pathname || href === parentHref) return false;
  if (!query) return !search.get("skill");
  return [...new URLSearchParams(query)].every(([k, v]) => search.get(k) === v);
}

export default function Sidebar() {
  return (
    <Suspense fallback={<aside className="sidebar" />}>
      <SidebarNav />
    </Suspense>
  );
}

function SidebarNav() {
  const pathname = usePathname();
  const search = useSearchParams();

  return (
    <aside className="sidebar">
      <div className="brand">
        <span className="logo">◆</span> StockScan
      </div>
      <nav className="side-nav">
        {items.map((item) => (
          <div key={item.href} className="nav-group">
            <Link href={item.href} className={pathname === item.href ? "active" : ""}>
              <span className="icon">{item.icon}</span>
              {item.label}
            </Link>
            {item.children?.map((child) => (
              <Link
                key={child.href}
                href={child.href}
                className={`sub ${isActive(pathname, search, child.href, item.href) ? "active" : ""}`}
              >
                {child.label}
              </Link>
            ))}
          </div>
        ))}
        <a href="/docs" target="_blank" rel="noreferrer">
          <span className="icon">{"{}"}</span>API Docs
        </a>
      </nav>
      <form action="/api/auth/logout" method="post" className="signout">
        <button type="submit">Sign out</button>
      </form>
    </aside>
  );
}
