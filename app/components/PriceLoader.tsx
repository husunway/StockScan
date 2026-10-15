"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

type PriceRow = {
  trade_date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  adj_close: number | null;
  volume: number | null;
};

type LoadResult = {
  symbol: string;
  rows_saved: number;
  start_date: string;
  end_date: string;
  table: string;
  preview: PriceRow[];
};

const fmt = (n: number | null, digits = 2) =>
  n === null ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: digits });

export default function PriceLoader() {
  const params = useSearchParams();
  const [symbol, setSymbol] = useState(params.get("symbol")?.toUpperCase() ?? "");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<LoadResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load(sym: string) {
    const s = sym.trim();
    if (!s) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch("/api/py/prices/load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: s }),
      });
      const data = await res.json();
      if (!res.ok) {
        const detail = data.detail;
        throw new Error(typeof detail === "string" ? detail : "Request failed");
      }
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }

  // Auto-load when arriving from the dashboard search (?symbol=XYZ).
  useEffect(() => {
    const s = params.get("symbol");
    if (s) load(s);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="loader">
      <form
        className="search-form"
        onSubmit={(e) => {
          e.preventDefault();
          load(symbol);
        }}
      >
        <input
          value={symbol}
          onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          placeholder="Ticker symbol, e.g. AAPL or RY.TO"
          maxLength={20}
          aria-label="Ticker symbol"
        />
        <button type="submit" disabled={loading || !symbol.trim()}>
          {loading ? "Loading…" : "Load 2-year history"}
        </button>
      </form>

      {loading && (
        <p className="msg">Fetching from Yahoo Finance and saving to Azure SQL…</p>
      )}
      {error && <p className="msg error">{error}</p>}

      {result && (
        <div className="result">
          <div className="stats">
            <div className="stat">
              <span className="stat-label">Symbol</span>
              <span className="stat-value">{result.symbol}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Rows saved</span>
              <span className="stat-value">{result.rows_saved.toLocaleString()}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Date range</span>
              <span className="stat-value small">
                {result.start_date} → {result.end_date}
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">Table</span>
              <span className="stat-value small">{result.table}</span>
            </div>
          </div>

          <h2 className="section-title">Latest rows</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th className="num">Open</th>
                  <th className="num">High</th>
                  <th className="num">Low</th>
                  <th className="num">Close</th>
                  <th className="num">Adj Close</th>
                  <th className="num">Volume</th>
                </tr>
              </thead>
              <tbody>
                {result.preview.map((r) => (
                  <tr key={r.trade_date}>
                    <td>{r.trade_date}</td>
                    <td className="num">{fmt(r.open)}</td>
                    <td className="num">{fmt(r.high)}</td>
                    <td className="num">{fmt(r.low)}</td>
                    <td className="num">{fmt(r.close)}</td>
                    <td className="num">{fmt(r.adj_close)}</td>
                    <td className="num">{fmt(r.volume, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
