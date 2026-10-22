"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

type Reference = { type: string; title: string; detail: string; used_for: string };
type Decision = {
  decision_id: number;
  backtest_id: number;
  skill_id: string;
  symbol: string;
  as_of_date: string;
  status: string;
  recommendation: "BUY" | "HOLD" | "SELL" | null;
  conviction: string | null;
  current_price: number | null;
  buy_below_price: number | null;
  target_price: number | null;
  stop_price: number | null;
  intrinsic_value_low: number | null;
  intrinsic_value_base: number | null;
  intrinsic_value_high: number | null;
  margin_of_safety_pct: number | null;
  headline: string | null;
  summary: string | null;
  error: string | null;
  warnings: string[];
  references: Reference[];
  report_stored: boolean;
  report_html_stored?: boolean;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  finished_at: string | null;
  input: unknown;
  output: Record<string, unknown> | null;
};

const money = (v: number | null | undefined, ccy: string) =>
  v === null || v === undefined ? "—" : `${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${ccy}`;
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`);

export default function DecisionView() {
  const params = useSearchParams();
  const bt = params.get("bt");
  const id = params.get("id");
  const [d, setD] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"html" | "pdf" | "output" | "input">("html");

  useEffect(() => {
    if (!bt || !id) {
      setError("Missing backtest or decision id.");
      return;
    }
    fetch(`/api/py/agent/backtest/${bt}/decision/${id}`)
      .then(async (r) => {
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Could not load decision");
        setD(data);
      })
      .catch((e: Error) => setError(e.message));
  }, [bt, id]);

  if (error) return <p className="msg error">{error}</p>;
  if (!d) return <p className="msg">Loading…</p>;

  const out = d.output ?? {};
  const ccy = typeof out.currency === "string" ? out.currency : "";
  const reportUrl = `/api/py/agent/backtest/${d.backtest_id}/decision/${d.decision_id}/report.pdf`;
  const htmlUrl = `/api/py/agent/backtest/${d.backtest_id}/decision/${d.decision_id}/report.html`;
  const upside = d.target_price && d.current_price ? (d.target_price / d.current_price - 1) * 100 : null;
  const downside = d.stop_price && d.current_price ? (d.stop_price / d.current_price - 1) * 100 : null;

  return (
    <div className="single">
      <p className="msg hint">
        <Link href={`/agent/backtest?skill=${d.skill_id}&bt=${d.backtest_id}`}>← Back to backtest #{d.backtest_id}</Link>
      </p>

      <div className="stats" style={{ marginBottom: 14 }}>
        <div className="stat"><span className="stat-label">Run date (as of)</span><span className="stat-value">{d.as_of_date}</span></div>
        <div className="stat"><span className="stat-label">Symbol</span><span className="stat-value">{d.symbol}</span></div>
        <div className="stat"><span className="stat-label">Skill</span><span className="stat-value small">{d.skill_id}</span></div>
        <div className="stat"><span className="stat-label">Completed</span><span className="stat-value small">{d.finished_at ? new Date(d.finished_at + "Z").toLocaleString() : d.status}</span></div>
      </div>

      {d.error && <p className="msg error">{d.error}</p>}

      {d.output && (
        <section className={`verdict rec-${(d.recommendation ?? "").toLowerCase()}`}>
          <div className="verdict-head">
            <span className="rec-badge">{d.recommendation}</span>
            <div>
              <h2>{d.headline}</h2>
              <p className="verdict-meta">
                Conviction <strong>{d.conviction}</strong> · Horizon {String(out.time_horizon ?? "—")} · Margin of safety{" "}
                <strong>{pct(d.margin_of_safety_pct)}</strong>
                {typeof out.required_margin_of_safety_pct === "number" && <> (required {out.required_margin_of_safety_pct.toFixed(0)}%)</>}
              </p>
            </div>
          </div>
          <div className="price-grid">
            <div className="stat"><span className="stat-label">Price on run date</span><span className="stat-value">{money(d.current_price, ccy)}</span></div>
            <div className="stat">
              <span className="stat-label">Intrinsic value (base)</span>
              <span className="stat-value">{money(d.intrinsic_value_base, ccy)}</span>
              <span className="tile-note">Range {money(d.intrinsic_value_low, ccy)} – {money(d.intrinsic_value_high, ccy)}</span>
            </div>
            <div className="stat"><span className="stat-label">Buy below</span><span className="stat-value">{money(d.buy_below_price, ccy)}</span></div>
            <div className={`stat ${d.recommendation !== "HOLD" ? "emph" : ""}`}>
              <span className="stat-label">Target price</span>
              <span className="stat-value">{money(d.target_price, ccy)}</span>
              {upside !== null && <span className="tile-note">{pct(upside)} from price</span>}
            </div>
            <div className={`stat ${d.recommendation === "BUY" ? "emph" : ""}`}>
              <span className="stat-label">Stop price</span>
              <span className="stat-value">{money(d.stop_price, ccy)}</span>
              {downside !== null && <span className="tile-note">{pct(downside)} from price</span>}
            </div>
          </div>
          {d.warnings.length > 0 && <ul className="warnings">{d.warnings.map((w) => <li key={w}>⚠ {w}</li>)}</ul>}
          {d.summary && (
            <div style={{ marginTop: 14 }}>
              {d.summary.split(/\n{2,}/).map((p, i) => <p key={i} className="prose">{p}</p>)}
            </div>
          )}
        </section>
      )}

      {d.references.length > 0 && (
        <>
          <h2 className="section-title">Reports and data referenced</h2>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Source</th><th>Detail</th><th>Used for</th></tr></thead>
              <tbody>
                {d.references.map((r, i) => (
                  <tr key={i}>
                    <td>{r.title}</td>
                    <td className="wrap">{r.detail}</td>
                    <td className="wrap">{r.used_for}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {d.output && (
        <>
          <div className="seg" role="tablist" style={{ margin: "20px 0 10px" }}>
            <button type="button" className={tab === "html" ? "on" : ""} onClick={() => setTab("html")}>Interactive report (HTML)</button>
            <button type="button" className={tab === "pdf" ? "on" : ""} onClick={() => setTab("pdf")}>PDF report</button>
            <button type="button" className={tab === "output" ? "on" : ""} onClick={() => setTab("output")}>Full output (JSON)</button>
            <button type="button" className={tab === "input" ? "on" : ""} onClick={() => setTab("input")}>Data the agent saw</button>
          </div>
          {tab === "html" ? (
            <>
              <p className="msg hint" style={{ marginTop: 0 }}>
                <a href={htmlUrl} target="_blank" rel="noreferrer">Open in a new tab</a> ·{" "}
                {d.report_html_stored ? "stored in Vercel Blob" : "generated on demand"} · chart and evidence use only data dated on or before {d.as_of_date}
              </p>
              <iframe className="pdf-frame report-frame" src={htmlUrl} title={`Interactive report ${d.symbol} ${d.as_of_date}`} />
            </>
          ) : tab === "pdf" ? (
            <>
              <p className="msg hint" style={{ marginTop: 0 }}>
                <a href={reportUrl} target="_blank" rel="noreferrer">Open in a new tab</a> ·{" "}
                <a href={reportUrl} download>Download</a> ·{" "}
                {d.report_stored ? "stored in Vercel Blob" : "generated on demand (no Blob store configured when this run completed)"} ·{" "}
                {d.model} · {d.input_tokens?.toLocaleString()} in / {d.output_tokens?.toLocaleString()} out tokens
              </p>
              <iframe className="pdf-frame" src={reportUrl} title={`Report ${d.symbol} ${d.as_of_date}`} />
            </>
          ) : (
            <pre className="data-dump" style={{ maxHeight: 640 }}>
              {JSON.stringify(tab === "output" ? d.output : d.input, null, 2)}
            </pre>
          )}
        </>
      )}
    </div>
  );
}
