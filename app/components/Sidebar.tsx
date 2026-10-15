"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const items = [
  { href: "/", label: "Dashboard", icon: "▦" },
  { href: "/prices", label: "Load Prices", icon: "↓" },
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
          <Link
            key={item.href}
            href={item.href}
            className={pathname === item.href ? "active" : ""}
          >
            <span className="icon">{item.icon}</span>
            {item.label}
          </Link>
        ))}
        <a href="/docs" target="_blank" rel="noreferrer">
          <span className="icon">{"{}"}</span>API Docs
        </a>
      </nav>
    </aside>
  );
}
