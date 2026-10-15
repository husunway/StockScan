"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";

const sections = [
  { title: "Price Chart", body: "Two-year daily close with volume." },
  { title: "Returns", body: "Daily, monthly and annualized returns." },
  { title: "Moving Averages", body: "20, 50 and 200-day trends and crossovers." },
  { title: "Volatility & Drawdown", body: "Rolling volatility and maximum drawdown." },
];

export default function AnalysisPanel() {
  const params = useSearchParams();
  const [symbol, setSymbol] = useState(params.get("symbol")?.toUpperCase() ?? "");
  const [selected, setSelected] = useState<string | null>(null);

  return (
    <div className="loader">
      <form
        className="search-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (symbol.trim()) setSelected(symbol.trim());
        }}
      >
        <input
          value={symbol}
          onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          placeholder="Ticker symbol, e.g. AAPL or RY.TO"
          maxLength={20}
          aria-label="Ticker symbol"
        />
        <button type="submit" disabled={!symbol.trim()}>
          Analyze
        </button>
      </form>

      {selected && (
        <p className="msg">
          Analysis for <strong>{selected}</strong> is not implemented yet.
        </p>
      )}

      <section className="grid result">
        {sections.map((s) => (
          <div key={s.title} className="card">
            <h2>{s.title}</h2>
            <p>{s.body}</p>
            <span className="badge">Coming soon</span>
          </div>
        ))}
      </section>
    </div>
  );
}
