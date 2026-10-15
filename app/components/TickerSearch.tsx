"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export default function TickerSearch() {
  const router = useRouter();
  const [ticker, setTicker] = useState("");

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const symbol = ticker.trim();
    if (!symbol) return;
    router.push(`/prices?symbol=${encodeURIComponent(symbol)}`);
  }

  return (
    <div className="search">
      <form onSubmit={onSubmit} className="search-form">
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="e.g. AAPL, MSFT, RY.TO"
          maxLength={20}
          aria-label="Ticker symbol"
        />
        <button type="submit" disabled={!ticker.trim()}>
          Load Prices
        </button>
      </form>
    </div>
  );
}
