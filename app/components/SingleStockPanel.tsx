"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";
import Link from "next/link";
import { type Analysis, MA_COLORS, MA_PERIODS } from "./analysisTypes";
import type { Range } from "./StockChart";

const StockChart = dynamic(() => import("./StockChart"), { ssr: false });

type SymbolInfo = { symbol: string; rows: number; start_date: string; end_date: string };
type Tone = "good" | "bad" | "warn" | "neutral";
type Tile = { label: string; value: string; state: string; tone: Tone; note?: string };

const INDEX_DEFAULTS = ["SPY", "^GSPC", "^GSPTSE", "XIU.TO", "QQQ", "VOO", "IVV"];
const RANGES: Range[] = ["3m", "6m", "1y", "all"];
const ICON: Record<Tone, string> = { good: "▲", bad: "▼", warn: "!", neutral: "●" };

const n = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);

function buildTiles(a: Analysis): Tile[] {
  const s = a.signals;
  const tiles: Tile[] = [];

  tiles.push({
    label: "Trend (MA 20/50/200)",
    value: n(s.last_close),
    state: s.ma.alignment,
    tone: s.ma.alignment === "bullish" ? "good" : s.ma.alignment === "bearish" ? "bad" : "neutral",
    note: s.ma.last_crossover
      ? `Last cross: ${s.ma.last_crossover.pair} ${s.ma.last_crossover.type} on ${s.ma.last_crossover.date}`
      : "No crossovers in range",
  });
  tiles.push({
    label: "RSI (14)",
    value: n(s.rsi.value, 1),
    state: s.rsi.state,
    tone: s.rsi.state === "neutral" ? "neutral" : "warn",
    note: "Overbought ≥ 70 · Oversold ≤ 30",
  });
  tiles.push({
    label: "MACD (12, 26, 9)",
    value: n(s.macd.histogram, 3),
    state: `${s.macd.state}, momentum ${s.macd.momentum}`,
    tone: s.macd.state === "bullish" ? "good" : "bad",
    note: `MACD ${n(s.macd.macd, 3)} · Signal ${n(s.macd.signal, 3)}`,
  });
  tiles.push({
    label: "Bollinger (20, 2σ)",
    value: `%B ${n(s.bollinger.pct_b)}`,
    state: s.bollinger.squeeze ? `${s.bollinger.state}, squeeze` : s.bollinger.state,
    tone: s.bollinger.state === "inside" ? "neutral" : "warn",
    note: `Bandwidth ${n(s.bollinger.bandwidth, 1)}%`,
  });
  const vc = s.volume.last_confirmation;
  tiles.push({
    label: "Volume",
    value: `${n(s.volume.last_ratio)}× avg`,
    state: `20-day trend ${s.volume.trend}`,
    tone: s.volume.trend === "rising" ? "good" : s.volume.trend === "falling" ? "bad" : "neutral",
    note: vc
      ? `${vc.date}: ${pct(vc.return_pct)} move ${vc.confirmed ? "confirmed" : "not confirmed"} (${n(vc.volume_ratio)}× vol)`
      : "No ±2% moves in last 60 days",
  });
  for (const [label, rs] of [["index", a.relative_strength.index], ["sector", a.relative_strength.sector]] as const) {
    if (!rs) continue;
    const w3 = rs.windows["3m"];
    tiles.push({
      label: `Relative strength vs ${label} (${rs.benchmark})`,
      value: w3 ? pct(w3.excess) : "—",
      state: rs.state,
      tone: rs.state === "outperforming" ? "good" : rs.state === "underperforming" ? "bad" : "neutral",
      note: w3 ? `3m: stock ${pct(w3.stock)} vs ${pct(w3.benchmark)} · RS 20d ${pct(rs.rs_20d_change)}` : "Not enough overlap",
    });
  }
  return tiles;
}

export default function SingleStockPanel() {
  const params = useSearchParams();
  const [symbols, setSymbols] = useState<SymbolInfo[] | null>(null);
  const [symbol, setSymbol] = useState(params.get("symbol")?.toUpperCase() ?? "");
  const [index, setIndex] = useState("");
  const [sector, setSector] = useState("");
  const [visible, setVisible] = useState<Set<string>>(new Set(["20", "50", "200"]));
  const [range, setRange] = useState<Range>("1y");
  const [data, setData] = useState<Analysis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dark, setDark] = useState(false);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    setDark(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setDark(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    fetch("/api/py/symbols")
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not list symbols");
        return d.symbols as SymbolInfo[];
      })
      .then((list) => {
        setSymbols(list);
        const names = list.map((s) => s.symbol);
        setSymbol((prev) => (prev && names.includes(prev) ? prev : names[0] ?? ""));
        setIndex((prev) => prev || INDEX_DEFAULTS.find((d) => names.includes(d)) || "");
      })
      .catch((e: Error) => {
        setSymbols([]);
        setError(e.message);
      });
  }, []);

  useEffect(() => {
    if (!symbol) return;
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    const q = new URLSearchParams({ symbol });
    if (index) q.set("index", index);
    if (sector) q.set("sector", sector);
    fetch(`/api/py/analysis/single?${q}`, { signal: ctrl.signal })
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Request failed");
        setData(d as Analysis);
        setLoading(false);
      })
      .catch((e: Error) => {
        if (e.name === "AbortError") return;
        setError(e.message);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [symbol, index, sector]);

  function toggleMA(p: string) {
    setVisible((prev) => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });
  }

  if (symbols && symbols.length === 0 && !error) {
    return (
      <p className="msg">
        No symbols loaded yet. Load one on the <Link href="/prices">Load Prices</Link> page first.
      </p>
    );
  }

  const benchOptions = symbols?.filter((s) => s.symbol !== symbol) ?? [];
  const tiles = data ? buildTiles(data) : [];
  const mode = dark ? "dark" : "light";

  return (
    <div className="single">
      <div className="filters">
        <label>
          Symbol
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)} disabled={!symbols}>
            {symbols?.map((s) => (
              <option key={s.symbol} value={s.symbol}>
                {s.symbol} ({s.rows} days)
              </option>
            ))}
          </select>
        </label>
        <label>
          Index benchmark
          <select value={index} onChange={(e) => setIndex(e.target.value)} disabled={!symbols}>
            <option value="">None</option>
            {benchOptions.map((s) => (
              <option key={s.symbol} value={s.symbol}>{s.symbol}</option>
            ))}
          </select>
        </label>
        <label>
          Sector benchmark
          <select value={sector} onChange={(e) => setSector(e.target.value)} disabled={!symbols}>
            <option value="">None</option>
            {benchOptions.map((s) => (
              <option key={s.symbol} value={s.symbol}>{s.symbol}</option>
            ))}
          </select>
        </label>
        <div className="seg" role="group" aria-label="Time range">
          {RANGES.map((r) => (
            <button key={r} type="button" className={r === range ? "on" : ""} onClick={() => setRange(r)}>
              {r.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
      {!index && !sector && symbols && symbols.length > 0 && (
        <p className="msg hint">
          For relative strength, load an index (e.g. SPY or ^GSPTSE) and a sector ETF (e.g. XLK) on the
          Load Prices page, then pick them above.
        </p>
      )}

      {error && <p className="msg error">{error}</p>}

      {data && (
        <div className={loading ? "dim" : ""}>
          <div className="tiles">
            {tiles.map((t) => (
              <div key={t.label} className={`tile tone-${t.tone}`}>
                <span className="stat-label">{t.label}</span>
                <span className="tile-value">{t.value}</span>
                <span className="tile-state">
                  <span className="tile-icon" aria-hidden>{ICON[t.tone]}</span> {t.state}
                </span>
                {t.note && <span className="tile-note">{t.note}</span>}
              </div>
            ))}
          </div>

          <div className="toggles" role="group" aria-label="Moving averages">
            {MA_PERIODS.map((p) => (
              <label key={p} className="toggle">
                <input type="checkbox" checked={visible.has(p)} onChange={() => toggleMA(p)} />
                <i className="key" style={{ background: MA_COLORS[p][mode] }} />
                MA{p}
              </label>
            ))}
            <span className="legend-note">
              <i className="key dotted" /> Bollinger · <span className="sr s">- - -</span> support ·{" "}
              <span className="sr r">- - -</span> resistance · ▲▼ MA crossovers
            </span>
          </div>

          <StockChart data={data} visibleMAs={visible} dark={dark} range={range} />

          <div className="details">
            <div>
              <h2 className="section-title">Support &amp; resistance (1y pivots)</h2>
              {data.levels.length === 0 ? (
                <p className="msg">No clear levels found.</p>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr><th>Type</th><th className="num">Level</th><th className="num">Touches</th><th>Last touch</th></tr>
                    </thead>
                    <tbody>
                      {data.levels.map((l) => (
                        <tr key={`${l.type}-${l.level}`}>
                          <td>{l.type}</td>
                          <td className="num">{n(l.level)}</td>
                          <td className="num">{l.touches}</td>
                          <td>{l.last_date}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <div>
              <h2 className="section-title">Recent MA crossovers</h2>
              {data.crossovers.length === 0 ? (
                <p className="msg">None in range.</p>
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr><th>Date</th><th>Pair</th><th>Type</th><th className="num">Close</th></tr>
                    </thead>
                    <tbody>
                      {data.crossovers.slice(-6).reverse().map((c) => (
                        <tr key={`${c.date}-${c.pair}`}>
                          <td>{c.date}</td>
                          <td>{c.pair}</td>
                          <td>{c.type === "bullish" ? "▲ bullish" : "▼ bearish"}</td>
                          <td className="num">{n(c.price)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>

          {(data.relative_strength.index || data.relative_strength.sector) && (
            <>
              <h2 className="section-title">Relative performance</h2>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Benchmark</th>
                      {Object.keys(data.relative_strength.index?.windows ?? data.relative_strength.sector?.windows ?? {}).map((w) => (
                        <th key={w} className="num">{w} excess</th>
                      ))}
                      <th>RS trend</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[data.relative_strength.index, data.relative_strength.sector].filter(Boolean).map((rs) => (
                      <tr key={rs!.benchmark}>
                        <td>{rs!.benchmark}</td>
                        {Object.values(rs!.windows).map((w, i) => (
                          <td key={i} className="num">{pct(w.excess)}</td>
                        ))}
                        <td>{rs!.state}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <h2 className="section-title">Latest values</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th className="num">Close</th>
                  <th className="num">MA20</th>
                  <th className="num">MA50</th>
                  <th className="num">MA200</th>
                  <th className="num">RSI</th>
                  <th className="num">MACD</th>
                  <th className="num">Hist</th>
                  <th className="num">%B</th>
                  <th className="num">Vol ratio</th>
                </tr>
              </thead>
              <tbody>
                {data.table.map((r) => (
                  <tr key={r.date}>
                    <td>{r.date}</td>
                    <td className="num">{n(r.close)}</td>
                    <td className="num">{n(r.ma20)}</td>
                    <td className="num">{n(r.ma50)}</td>
                    <td className="num">{n(r.ma200)}</td>
                    <td className="num">{n(r.rsi, 1)}</td>
                    <td className="num">{n(r.macd, 3)}</td>
                    <td className="num">{n(r.histogram, 3)}</td>
                    <td className="num">{n(r.pct_b)}</td>
                    <td className="num">{n(r.volume_ratio)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="msg">
            Indicators are computed from stored daily closes and are informational only, not investment advice.
          </p>
        </div>
      )}
      {loading && !data && <p className="msg">Computing indicators…</p>}
    </div>
  );
}
