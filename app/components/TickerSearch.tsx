"use client";

import { useState } from "react";

type Result = { ticker: string; status: string; message: string };

export default function TickerSearch() {
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const symbol = ticker.trim();
    if (!symbol) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch(`/api/py/stock/${encodeURIComponent(symbol)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail ?? "Request failed");
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="search">
      <form onSubmit={onSubmit} className="search-form">
        <input
          value={ticker}
          onChange={(e) => setTicker(e.target.value.toUpperCase())}
          placeholder="e.g. AAPL, MSFT, RY.TO"
          maxLength={10}
          aria-label="Ticker symbol"
        />
        <button type="submit" disabled={loading || !ticker.trim()}>
          {loading ? "Analyzing…" : "Analyze"}
        </button>
      </form>
      {error && <p className="msg error">{error}</p>}
      {result && <p className="msg">{result.message}</p>}
    </div>
  );
}
