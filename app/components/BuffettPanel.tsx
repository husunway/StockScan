"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";

type Checklist = { criterion: string; verdict: "pass" | "fail" | "unclear"; evidence: string };
type Metric = { name: string; value: string; assessment: string; source: string };

type Analysis = {
  recommendation: "BUY" | "HOLD" | "SELL";
  conviction: "high" | "medium" | "low";
  headline: string;
  currency: string;
  current_price: number;
  intrinsic_value_low: number;
  intrinsic_value_base: number;
  intrinsic_value_high: number;
  margin_of_safety_pct: number;
  required_margin_of_safety_pct: number;
  buy_below_price: number;
  target_price: number | null;
  stop_price: number | null;
  target_rationale: string;
  stop_rationale: string;
  time_horizon: string;
  summary: string;
  checklist: Checklist[];
  sections: { title: string; content: string }[];
  key_metrics: Metric[];
  valuation: {
    method: string;
    owner_earnings: string;
    growth_rate_pct_years_1_5: number;
    growth_rate_pct_years_6_10: number;
    discount_rate_pct: number;
    terminal_growth_pct: number;
    notes: string;
  };
  risks: string[];
  assumptions: string[];
  data_gaps: string[];
};

type Result = {
  symbol: string;
  analysis: Analysis;
  warnings: string[];
  model: string;
  generated_at: string;
  usage: { input_tokens: number; output_tokens: number; cache_read_input_tokens: number | null };
};

const money = (v: number | null | undefined, ccy: string) =>
  v === null || v === undefined
    ? "—"
    : `${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${ccy}`;
const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`;
const VERDICT_ICON = { pass: "✓", fail: "✗", unclear: "?" } as const;

export default function BuffettPanel() {
  const params = useSearchParams();
  const [symbol, setSymbol] = useState(params.get("symbol")?.toUpperCase() ?? "");
  const [known, setKnown] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [showData, setShowData] = useState(false);

  useEffect(() => {
    fetch("/api/py/symbols")
      .then((r) => (r.ok ? r.json() : { symbols: [] }))
      .then((d) => setKnown((d.symbols ?? []).map((s: { symbol: string }) => s.symbol)))
      .catch(() => {});
  }, []);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    const s = symbol.trim();
    if (!s) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch("/api/py/ai/buffett", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: s }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`);
      setResult(data as Result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setLoading(false);
    }
  }

  const a = result?.analysis;
  const ccy = a?.currency ?? "";
  const upside = a && a.target_price ? (a.target_price / a.current_price - 1) * 100 : null;
  const downside = a && a.stop_price ? (a.stop_price / a.current_price - 1) * 100 : null;

  return (
    <div className="single">
      <form className="search-form buffett-form" onSubmit={run}>
        <input
          list="known-symbols"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value.toUpperCase())}
          placeholder="Ticker symbol, e.g. KO, AAPL, RY.TO"
          maxLength={20}
          aria-label="Ticker symbol"
        />
        <datalist id="known-symbols">
          {known.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
        <button type="submit" disabled={loading || !symbol.trim()}>
          {loading ? "Analyzing…" : "Analyze"}
        </button>
      </form>
      <p className="msg hint">
        Fundamentals come from Yahoo Finance; prices from your loaded data (or Yahoo if the
        symbol is not loaded). The analysis usually takes 30–90 seconds.
      </p>

      {error && <p className="msg error">{error}</p>}
      {loading && (
        <div className="working">
          <span className="spinner" aria-hidden /> Gathering data and running the Buffett framework…
        </div>
      )}

      {result && a && (
        <div className="buffett">
          <section className={`verdict rec-${a.recommendation.toLowerCase()}`}>
            <div className="verdict-head">
              <span className="rec-badge">{a.recommendation}</span>
              <div>
                <h2>
                  {result.symbol}: {a.headline}
                </h2>
                <p className="verdict-meta">
                  Conviction <strong>{a.conviction}</strong> · Horizon {a.time_horizon} · Margin of safety{" "}
                  <strong>{pct(a.margin_of_safety_pct)}</strong> (required {a.required_margin_of_safety_pct.toFixed(0)}%)
                </p>
              </div>
            </div>
            <div className="price-grid">
              <div className="stat">
                <span className="stat-label">Current price</span>
                <span className="stat-value">{money(a.current_price, ccy)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Intrinsic value (base)</span>
                <span className="stat-value">{money(a.intrinsic_value_base, ccy)}</span>
                <span className="tile-note">
                  Range {money(a.intrinsic_value_low, ccy)} – {money(a.intrinsic_value_high, ccy)}
                </span>
              </div>
              <div className="stat">
                <span className="stat-label">Buy below</span>
                <span className="stat-value">{money(a.buy_below_price, ccy)}</span>
              </div>
              <div className={`stat ${a.recommendation === "BUY" ? "emph" : ""}`}>
                <span className="stat-label">Target price (sell)</span>
                <span className="stat-value">{money(a.target_price, ccy)}</span>
                {upside !== null && <span className="tile-note">{pct(upside)} from current</span>}
              </div>
              <div className={`stat ${a.recommendation === "BUY" ? "emph" : ""}`}>
                <span className="stat-label">Stop price</span>
                <span className="stat-value">{money(a.stop_price, ccy)}</span>
                {downside !== null && <span className="tile-note">{pct(downside)} from current</span>}
              </div>
            </div>
            {(a.target_rationale || a.stop_rationale) && (
              <div className="rationale">
                {a.target_rationale && (
                  <p>
                    <strong>Target:</strong> {a.target_rationale}
                  </p>
                )}
                {a.stop_rationale && (
                  <p>
                    <strong>Stop:</strong> {a.stop_rationale}
                  </p>
                )}
              </div>
            )}
            {result.warnings.length > 0 && (
              <ul className="warnings">
                {result.warnings.map((w) => (
                  <li key={w}>⚠ {w}</li>
                ))}
              </ul>
            )}
          </section>

          <h2 className="section-title">Summary</h2>
          {a.summary.split(/\n{2,}/).map((p, i) => (
            <p key={i} className="prose">
              {p}
            </p>
          ))}

          <h2 className="section-title">Buffett checklist</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Criterion</th>
                  <th>Verdict</th>
                  <th>Evidence</th>
                </tr>
              </thead>
              <tbody>
                {a.checklist.map((c) => (
                  <tr key={c.criterion}>
                    <td>{c.criterion}</td>
                    <td className={`verdict-${c.verdict}`}>
                      {VERDICT_ICON[c.verdict]} {c.verdict}
                    </td>
                    <td className="wrap">{c.evidence}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {a.sections.map((s) => (
            <section key={s.title}>
              <h2 className="section-title">{s.title}</h2>
              {s.content.split(/\n{2,}/).map((p, i) => (
                <p key={i} className="prose">
                  {p}
                </p>
              ))}
            </section>
          ))}

          <h2 className="section-title">Valuation inputs</h2>
          <div className="table-wrap">
            <table className="table">
              <tbody>
                <tr><td>Method</td><td className="wrap">{a.valuation.method}</td></tr>
                <tr><td>Owner earnings</td><td className="wrap">{a.valuation.owner_earnings}</td></tr>
                <tr><td>Growth, years 1–5</td><td>{a.valuation.growth_rate_pct_years_1_5.toFixed(1)}%</td></tr>
                <tr><td>Growth, years 6–10</td><td>{a.valuation.growth_rate_pct_years_6_10.toFixed(1)}%</td></tr>
                <tr><td>Discount rate</td><td>{a.valuation.discount_rate_pct.toFixed(1)}%</td></tr>
                <tr><td>Terminal growth</td><td>{a.valuation.terminal_growth_pct.toFixed(1)}%</td></tr>
                <tr><td>Notes</td><td className="wrap">{a.valuation.notes}</td></tr>
              </tbody>
            </table>
          </div>

          <h2 className="section-title">Key metrics</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Metric</th>
                  <th>Value</th>
                  <th>Assessment</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {a.key_metrics.map((m) => (
                  <tr key={m.name}>
                    <td>{m.name}</td>
                    <td>{m.value}</td>
                    <td className="wrap">{m.assessment}</td>
                    <td><code>{m.source}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="details">
            <div>
              <h2 className="section-title">Risks</h2>
              <ul className="list">{a.risks.map((r) => <li key={r}>{r}</li>)}</ul>
            </div>
            <div>
              <h2 className="section-title">Assumptions (model judgment, not data)</h2>
              <ul className="list">{a.assumptions.map((r) => <li key={r}>{r}</li>)}</ul>
            </div>
          </div>
          {a.data_gaps.length > 0 && (
            <>
              <h2 className="section-title">Data gaps</h2>
              <ul className="list">{a.data_gaps.map((r) => <li key={r}>{r}</li>)}</ul>
            </>
          )}

          <p className="msg">
            Generated {new Date(result.generated_at).toLocaleString()} by {result.model} ·{" "}
            {result.usage.input_tokens.toLocaleString()} in / {result.usage.output_tokens.toLocaleString()} out tokens
            {" · "}
            <button type="button" className="linklike" onClick={() => setShowData((v) => !v)}>
              {showData ? "Hide" : "Show"} data sent to the model
            </button>
          </p>
          {showData && (
            <pre className="data-dump">{JSON.stringify((result as unknown as { data_used: unknown }).data_used, null, 2)}</pre>
          )}
          <p className="msg disclaimer">
            This is model-generated analysis based on third-party data that may be incomplete or out of date.
            It is not investment advice. Verify the figures and make your own decision.
          </p>
        </div>
      )}
    </div>
  );
}
