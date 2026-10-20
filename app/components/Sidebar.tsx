"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

type Item = { href: string; label: string; icon?: string; children?: Item[] };

const items: Item[] = [
  { href: "/", label: "Dashboard", icon: "▦" },
  { href: "/prices", label: "Load Prices", icon: "↓" },
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
      { href: "/agent/run?skill=warren-buffett", label: "Warren Buffett" },
      { href: "/agent", label: "Skill library" },
    ],
  },
  { href: "/diagnosis", label: "Diagnosis", icon: "⚙" },
];

export default function Sidebar() {
  const pathname = usePathname();

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
                className={`sub ${pathname === child.href.split("?")[0] && child.href !== item.href ? "active" : ""}`}
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
