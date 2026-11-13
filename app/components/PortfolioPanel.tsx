"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, DiagnosticsPanel, LiveLog, type Diagnostics, type LogEntry } from "./RunUi";
import PortfolioCalendar from "./PortfolioCalendar";
import { IndexSelect, indexLabel, ModelSelect, type IndexId } from "./RunOptions";
import { PORTFOLIO_FIELDS } from "@/lib/portfolioFields";
import { defaultInputs, validateAll, validateField, type FieldSpec } from "@/lib/portfolioValidation";

type Config = {
  stock_count: number; start_date: string; end_date: string | null; starting_capital: number; buy_amount: number; max_buys_per_stock: number;
  take_profit_pct: number; analysis_validity_days: number; rerun_proximity_pct: number; skip_threshold_pct: number; held_reassess_days: number;
  index?: string; model?: string | null;
};
type Position = {
  symbol: string; buys: number; quantity: number; avg_cost: number; cost: number; price: number | null; market_value: number | null;
  unrealized_pnl: number | null; unrealized_pct: number | null; opened_date: string;
};
type Trade = {
  trade_id: number; trade_date: string; symbol: string; side: "BUY" | "SELL"; quantity: number; price: number; amount: number;
  reason: string | null; realized_pnl: number | null; cash_after: number;
};
type Assessment = {
  assessment_id: number; sim_date: string; symbol: string; skill_id: string; phase: string; mode: string; decision_id: number | null;
  recommendation: string | null; current_price: number | null; buy_price: number | null; stop_price: number | null; target_price: number | null;
  is_buy: boolean; note: string | null;
};
type Snapshot = { trade_date: string; cash: number; market_value: number; total_value: number; realized_pnl: number; positions: number };
type State = {
  job_id: number; created_at: string; status: "active" | "stopped" | "done"; start_date: string; end_date: string; config: Config; symbols: string[];
  days: string[]; backtests: Record<string, number>; cash: number; realized_pnl: number; ai_calls: number; failed_calls: number;
  days_total: number; days_done: number;
  progress: { idx: number; current_date: string | null; phase: string; stage: string; symbol: string | null; priced_as_of: string };
  positions: Position[]; market_value: number; total_value: number; unrealized_pnl: number; return_pct: number;
  trades: Trade[]; trade_counts: { total: number; buys: number; sells: number; wins: number; win_rate_pct: number | null };
  assessment_counts: Record<string, number>; assessments: Assessment[]; snapshots: Snapshot[]; log?: LogEntry[];
  trade_days: { trade_date: string; symbol: string; side: "BUY" | "SELL"; count: number; realized_pnl: number }[];
  ai_costs: { trade_date: string; cost: number; calls: number }[]; ai_cost_total: number; ai_cost_currency: string; ai_cost_unpriced: boolean;
  reused_calls: number;
};
type StepResult = { done: boolean; stopped: boolean; busy: boolean; model_call?: boolean; day_done?: boolean; state: State | null; log: LogEntry[] };
type SymbolInfo = { symbol: string; name: string | null; ready: boolean; price_last: string | null; tech_fresh: boolean; bundle_fetched_at: string | null };

const SKILL_NAME: Record<string, string> = { "warren-buffett": "Buffett", "jamie-dimon": "Dimon", "john-murphy": "Murphy" };
const POLL_MS = 5000;
const money = (v: number | null | undefined, nd = 2) => (v === null || v === undefined ? "—" : v.toLocaleString("en-CA", { minimumFractionDigits: nd, maximumFractionDigits: nd }));
const signed = (v: number | null | undefined, nd = 2) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${money(v, nd)}`);
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`);
const tone = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? "" : v > 0 ? "pos" : "neg");

const FORM_LABEL: Record<string, string> = {
  stock_count: "Stocks (first N by symbol)", starting_capital: "Starting capital (USD)", buy_amount: "Buy amount (USD)",
  max_buys_per_stock: "Max buys / stock", take_profit_pct: "Take profit %", analysis_validity_days: "Assessment valid (days)",
  rerun_proximity_pct: "Rerun within %", skip_threshold_pct: "Skip above %", held_reassess_days: "Held reassess (trading days)",
  parallel_calls: "Parallel agent calls",
};

/** A text input (not type=number) so the browser's step rules cannot reject valid values; checked by validateField. */
function NumField({ spec, label, value, error, disabled, onChange, onBlur }: {
  spec: FieldSpec; label: string; value: string; error?: string; disabled: boolean;
  onChange: (spec: FieldSpec, v: string) => void; onBlur: (spec: FieldSpec) => void;
}) {
  const id = `pf-${spec.key}`;
  return (
    <label htmlFor={id} className={error ? "invalid" : ""}>
      {label}
      <input id={id} type="text" inputMode={spec.kind === "decimal" ? "decimal" : "numeric"} autoComplete="off" value={value} disabled={disabled}
        aria-invalid={!!error} aria-describedby={error ? `${id}-err` : undefined}
        title={`${spec.kind === "decimal" ? "Number" : "Whole number"} from ${spec.min.toLocaleString("en-CA")} to ${spec.max.toLocaleString("en-CA")}`}
        onChange={(e) => onChange(spec, e.target.value)} onBlur={() => onBlur(spec)} />
      {error && <span className="field-error" id={`${id}-err`} role="alert">{error}</span>}
    </label>
  );
}

function aiCost(job: { ai_cost_total?: number; ai_cost_currency?: string }): string {
  const cur = job.ai_cost_currency ?? "USD";
  return `${cur === "USD" ? "US$" : `${cur} `}${(job.ai_cost_total ?? 0).toFixed(2)}`;
}

function Pill({ rec }: { rec: string | null }) {
  if (!rec) return <span className="tile-note">—</span>;
  return <span className={`rec-pill rec-${rec.toLowerCase()}`}>{rec}</span>;
}

export default function PortfolioPanel() {
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [diagError, setDiagError] = useState<string | null>(null);
  const [job, setJob] = useState<State | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [running, setRunning] = useState(false);
  const [preparing, setPreparing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Numeric inputs are kept as typed text and checked against data/portfolio_fields.json (the API applies the same rules).
  const [inputs, setInputs] = useState<Record<string, string>>(() => defaultInputs(PORTFOLIO_FIELDS));
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [index, setIndex] = useState<IndexId>("sp500");
  const [model, setModel] = useState("");
  const [startDate, setStartDate] = useState("2026-01-01");
  const [endDate, setEndDate] = useState("");
  const stopRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  const note = useCallback((message: string, level: LogEntry["level"] = "info") => {
    setLog((prev) => [...prev, { t: new Date().toISOString(), elapsed_s: 0, level, message }].slice(-800));
  }, []);
  const setInput = (spec: FieldSpec, v: string) => {
    setInputs((prev) => ({ ...prev, [spec.key]: v }));
    // Clear a message as soon as the value becomes valid; new messages appear on blur or submit.
    setFieldErrors((prev) => {
      if (!prev[spec.key] || !validateField(spec, v).ok) return prev;
      const next = { ...prev };
      delete next[spec.key];
      return next;
    });
  };
  const checkInput = (spec: FieldSpec) => {
    const r = validateField(spec, inputs[spec.key]);
    setFieldErrors((prev) => {
      const next = { ...prev };
      if (r.ok) delete next[spec.key]; else next[spec.key] = r.error;
      return next;
    });
  };
  const dateError = !startDate ? "From date is required." : endDate && endDate < startDate ? "To date must be on or after the From date." : null;

  useEffect(() => {
    api<Diagnostics>("/api/py/agent/diagnostics").then(setDiag).catch((e: Error) => setDiagError(e.message));
  }, []);

  const refresh = useCallback(async (id: number) => {
    try {
      const s = await api<State>(`/api/py/portfolio/${id}`);
      setJob((prev) => (prev && prev.job_id !== id ? prev : s));
    } catch {}
  }, []);
  const active = !!job && job.status === "active";
  useEffect(() => {
    if (!job || !active || running) return;
    const id = job.job_id;
    const t = setInterval(() => refresh(id), POLL_MS);
    return () => clearInterval(t);
  }, [job?.job_id, active, running, refresh]); // eslint-disable-line react-hooks/exhaustive-deps

  const runLoop = useCallback(async (id: number) => {
    stopRef.current = false;
    setRunning(true);
    setError(null);
    try {
      for (;;) {
        if (stopRef.current) return;
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        try {
          const r = await api<StepResult>(`/api/py/portfolio/${id}/step`, { method: "POST", signal: ctrl.signal });
          if (r.log.length) setLog((prev) => [...prev, ...r.log].slice(-800));
          if (r.state) setJob(r.state);
          if (r.busy) { await new Promise((res) => setTimeout(res, 3000)); continue; }
          if (r.done || r.stopped) return;
        } catch (e) {
          if (e instanceof DOMException && e.name === "AbortError") return;
          note(e instanceof Error ? e.message : "step failed", "error");
          await new Promise((res) => setTimeout(res, 4000)); // transient error: back off and keep going
        }
      }
    } finally {
      abortRef.current = null;
      setRunning(false);
      refresh(id);
    }
  }, [note, refresh]);

  async function start() {
    setError(null);
    const checked = validateAll(PORTFOLIO_FIELDS, inputs);
    setFieldErrors(checked.errors);
    if (!checked.ok || dateError) {
      setError(`Fix the highlighted field${Object.keys(checked.errors).length + (dateError ? 1 : 0) > 1 ? "s" : ""} before starting.`);
      const first = PORTFOLIO_FIELDS.find((f) => checked.errors[f.key]);
      if (first) document.getElementById(`pf-${first.key}`)?.focus();
      return;
    }
    const cfg = checked.values;
    setLog([]);
    setJob(null);
    stopRef.current = false;
    try {
      note(`Resolving the first ${cfg.stock_count} ${indexLabel(index)} constituents in symbol order ...`);
      const sy = await api<{ symbols: SymbolInfo[] }>(`/api/py/portfolio/symbols?count=${cfg.stock_count}&index=${index}`);
      note(`Stocks: ${sy.symbols.map((s) => s.symbol).join(", ")}`);
      // A stock that cannot be prepared (delisted, wrong ticker, Yahoo refusing) is left out with a warning;
      // the backtest runs on the stocks that were prepared.
      const prepared: string[] = [];
      const skipped: string[] = [];
      for (let i = 0; i < sy.symbols.length; i++) {
        if (stopRef.current) throw new Error("Cancelled before the backtest was created");
        const s = sy.symbols[i];
        setPreparing(`${s.symbol} (${i + 1}/${sy.symbols.length})`);
        try {
          const r = await api<{ prices: { action: string; added: number; last_date: string }; tech_last: string | null; log: LogEntry[] }>(
            "/api/py/portfolio/prepare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbol: s.symbol }) });
          note(`${s.symbol}: prices ${r.prices.action} (+${r.prices.added}, latest ${r.prices.last_date}); technical data to ${r.tech_last ?? "?"}; fundamentals bundle ready`);
          prepared.push(s.symbol);
        } catch (e) {
          skipped.push(s.symbol);
          note(`${s.symbol}: SKIPPED, left out of the backtest - ${e instanceof Error ? e.message : "could not prepare the data"}`, "warn");
        }
      }
      setPreparing(null);
      if (!prepared.length) throw new Error("No stock could be prepared; the backtest was not created.");
      if (skipped.length) note(`Backtest runs on ${prepared.length} of ${sy.symbols.length} stocks; left out: ${skipped.join(", ")}`, "warn");
      const body = { ...cfg, index, model: model || null, start_date: startDate, end_date: endDate || null, ...(skipped.length ? { symbols: prepared } : {}) };
      const s = await api<State>("/api/py/portfolio", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      setJob(s);
      if (s.log) setLog((prev) => [...prev, ...s.log!]);
      await runLoop(s.job_id);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Could not start the backtest";
      setError(msg);
      note(msg, "error");
      setPreparing(null);
    }
  }
  async function stop() {
    stopRef.current = true;
    abortRef.current?.abort();
    if (!job) { note("Stopping: data preparation will end after the current symbol.", "warn"); return; }
    try {
      const s = await api<State>(`/api/py/portfolio/${job.job_id}/stop`, { method: "POST" });
      setJob(s);
      note("Stopped. The step in flight (if any) finishes its model call and is kept; Resume continues from the saved cursor.", "warn");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not stop");
    }
  }
  async function resume() {
    if (!job) return;
    try {
      const s = await api<State>(`/api/py/portfolio/${job.job_id}/resume`, { method: "POST" });
      setJob(s);
      note(`Resumed from ${s.progress.current_date ?? "the end"} (${s.progress.phase} phase)`);
      await runLoop(s.job_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not resume");
    }
  }
  async function loadLast() {
    setError(null);
    try {
      const s = await api<State | null>("/api/py/portfolio/latest");
      if (!s) { setError("No portfolio backtest has been run yet."); return; }
      setJob(s);
      setLog(s.log ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the last run");
    }
  }

  const decisionLink = (skill: string, id: number | null) => (job && id ? `/agent/backtest/decision?bt=${job.backtests[skill]}&id=${id}` : null);
  const busy = running || !!preparing;
  const p = job?.progress;
  const progressPct = job && job.days_total ? (job.days_done / job.days_total) * 100 : 0;
  const cap = job?.config.starting_capital ?? Number(inputs.starting_capital);

  return (
    <div className="single" style={{ maxWidth: 1240 }}>
      <DiagnosticsPanel diag={diag} error={diagError} onLog={(entries) => setLog((prev) => [...prev, ...entries])} />

      <form className="filters agent-form portfolio-form" noValidate onSubmit={(e) => { e.preventDefault(); start(); }}>
        <IndexSelect value={index} onChange={setIndex} disabled={busy} />
        <ModelSelect value={model} onChange={setModel} disabled={busy} />
        {PORTFOLIO_FIELDS.slice(0, 1).map((f) => <NumField key={f.key} spec={f} label={FORM_LABEL[f.key]} value={inputs[f.key]} error={fieldErrors[f.key]} disabled={busy} onChange={setInput} onBlur={checkInput} />)}
        <label className={dateError && !startDate ? "invalid" : ""}>From<input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} disabled={busy} /></label>
        <label className={dateError && startDate ? "invalid" : ""}>To (blank = latest)<input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} disabled={busy} />
          {dateError && <span className="field-error" role="alert">{dateError}</span>}</label>
        {PORTFOLIO_FIELDS.slice(1).map((f) => <NumField key={f.key} spec={f} label={FORM_LABEL[f.key]} value={inputs[f.key]} error={fieldErrors[f.key]} disabled={busy} onChange={setInput} onBlur={checkInput} />)}
        <button type="submit" className="btn" disabled={busy}>{preparing ? `Preparing ${preparing}…` : running ? "Running…" : "Start backtest"}</button>
        <button type="button" className="btn small danger" disabled={!busy && !active} onClick={stop}>Stop</button>
        {job && !busy && job.status === "stopped" && <button type="button" className="btn small" onClick={resume}>Resume</button>}
        <button type="button" className="btn small ghost" disabled={busy} onClick={loadLast}>Load last run</button>
      </form>
      <p className="legend-note" style={{ marginTop: 0 }}>
        Each trading day: sells first (lowest agent target or take profit, full position), then buys. A buy needs Warren Buffett → Jamie Dimon →
        John Murphy to each recommend buying at the current price (early exit); Buffett and Dimon are cached for the validity window, rerun when
        the price is within the proximity band of their buy price, skipped (NOT BUY) when it is more than the skip threshold above it; Murphy
        always runs fresh. Fills at the lowest agent buy price if it lies inside the day&apos;s range. Amounts in USD (native currency of S&amp;P 500 and NASDAQ 100 prices).
      </p>
      {error && <p className="msg error">{error}</p>}

      {job && p && (
        <>
          <div className="progress-row">
            <div className="progress"><div className="progress-bar" style={{ width: `${progressPct}%` }} /></div>
            <span className="progress-text">
              Backtest #{job.job_id} · {indexLabel(job.config.index)}{job.config.model ? ` · ${job.config.model}` : ""} · {job.symbols.join(", ")} · day {Math.min(job.days_done + 1, job.days_total)} of {job.days_total}
              {p.current_date && <> · simulating <strong>{p.current_date}</strong> ({p.phase}{p.symbol ? ` · ${p.symbol}` : ""}{p.phase !== "close" && p.stage !== "check" ? ` · ${p.stage}` : ""})</>}
              {" "}· {job.ai_calls.toLocaleString()} model calls{job.failed_calls > 0 && <span style={{ color: "var(--danger, #c0392b)" }}> ({job.failed_calls} failed)</span>}
              {job.status === "stopped" && <span className="stopped-chip">stopped</span>}
              {job.status === "done" && <span className="stopped-chip" style={{ background: "#0ca30c" }}>complete</span>}
              {(running || active) && job.status === "active" && <span className="spinner inline" aria-hidden />}
            </span>
          </div>

          <div className="tiles">
            <div className="tile tone-neutral"><span className="stat-label">Cash</span><span className="tile-value">{money(job.cash, 0)}</span><span className="tile-note">of {money(cap, 0)} starting capital</span></div>
            <div className="tile tone-neutral"><span className="stat-label">Positions</span><span className="tile-value">{money(job.market_value, 0)}</span><span className="tile-note">{job.positions.length} stock(s) · priced as of {p.priced_as_of}</span></div>
            <div className={`tile ${job.total_value >= cap ? "tone-good" : "tone-bad"}`}><span className="stat-label">Total portfolio value</span><span className="tile-value">{money(job.total_value, 0)}</span><span className="tile-note">return {pct(job.return_pct)}</span></div>
            <div className={`tile ${job.realized_pnl > 0 ? "tone-good" : job.realized_pnl < 0 ? "tone-bad" : "tone-neutral"}`}><span className="stat-label">Realized P&amp;L</span><span className="tile-value">{signed(job.realized_pnl, 0)}</span><span className="tile-note">{job.trade_counts.sells} sell(s), {job.trade_counts.wins} profitable</span></div>
            <div className={`tile ${job.unrealized_pnl > 0 ? "tone-good" : job.unrealized_pnl < 0 ? "tone-bad" : "tone-neutral"}`}><span className="stat-label">Unrealized P&amp;L</span><span className="tile-value">{signed(job.unrealized_pnl, 0)}</span><span className="tile-note">on open positions</span></div>
            <div className="tile tone-neutral"><span className="stat-label">Estimated AI cost</span><span className="tile-value">{aiCost(job)}</span><span className="tile-note">{job.ai_calls.toLocaleString()} model call(s){job.reused_calls ? `, ${job.reused_calls} reused at no cost` : ""}{job.ai_cost_unpriced ? " · some models unpriced" : ""}</span></div>
            <div className="tile tone-neutral"><span className="stat-label">Agent runs</span><span className="tile-value">{job.ai_calls.toLocaleString()}</span><span className="tile-note">
              {["run", "rerun", "reassess", "cached", "skipped", "failed"].filter((m) => job.assessment_counts[m]).map((m) => `${m} ${job.assessment_counts[m]}`).join(" · ") || "none yet"}
            </span></div>
          </div>

          {job.status === "done" && (
            <div className="card" style={{ marginTop: 12 }}>
              <h2 className="section-title" style={{ marginTop: 0 }}>Summary — {job.days_total} trading days, {job.start_date} to {job.end_date}</h2>
              <div className="stats">
                <div className="stat"><span className="stat-label">Final value</span><span className="stat-value">{money(job.total_value, 0)}</span></div>
                <div className="stat"><span className="stat-label">Total return</span><span className={`stat-value ${tone(job.return_pct)}`}>{pct(job.return_pct)}</span></div>
                <div className="stat"><span className="stat-label">Trades</span><span className="stat-value">{job.trade_counts.total} ({job.trade_counts.buys} buys, {job.trade_counts.sells} sells)</span></div>
                <div className="stat"><span className="stat-label">Win rate</span><span className="stat-value">{job.trade_counts.win_rate_pct === null ? "— (no sells)" : `${job.trade_counts.win_rate_pct}%`}</span></div>
                <div className="stat"><span className="stat-label">Realized / unrealized</span><span className="stat-value">{signed(job.realized_pnl, 0)} / {signed(job.unrealized_pnl, 0)}</span></div>
                <div className="stat"><span className="stat-label">Model calls</span><span className="stat-value">{job.ai_calls}{job.failed_calls ? ` (${job.failed_calls} failed)` : ""}{job.reused_calls ? `, ${job.reused_calls} reused` : ""}</span></div>
                <div className="stat"><span className="stat-label">Estimated AI cost</span><span className="stat-value">{aiCost(job)}</span></div>
              </div>
            </div>
          )}
        </>
      )}

      {job && <>
        <h2 className="section-title">Calendar — daily portfolio change, buys and sells</h2>
        <PortfolioCalendar job={job} />
      </>}

      <LiveLog title={job ? `Simulation log - #${job.job_id}` : "Log"} entries={log} running={busy || active} waitHint="waiting for the next model call (30-90 s each); days without calls pass in seconds" />

      {job && (
        <>
          <h2 className="section-title">Positions — {job.positions.length}</h2>
          {job.positions.length === 0 ? <p className="msg">No open positions.</p> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Stock</th><th className="num">Buys</th><th className="num">Quantity</th><th className="num">Avg cost</th><th className="num">Current price</th><th className="num">Market value</th><th className="num">Unrealized P&amp;L</th><th className="num">%</th><th>Opened</th></tr></thead>
                <tbody>
                  {job.positions.map((ps) => (
                    <tr key={ps.symbol}>
                      <td><strong>{ps.symbol}</strong></td><td className="num">{ps.buys}</td><td className="num">{ps.quantity.toLocaleString()}</td>
                      <td className="num">{money(ps.avg_cost)}</td><td className="num">{money(ps.price)}</td><td className="num">{money(ps.market_value)}</td>
                      <td className={`num ${tone(ps.unrealized_pnl)}`}>{signed(ps.unrealized_pnl)}</td><td className={`num ${tone(ps.unrealized_pct)}`}>{pct(ps.unrealized_pct)}</td><td>{ps.opened_date}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2 className="section-title">Trade log — {job.trade_counts.total} trade(s){job.trades.length < job.trade_counts.total ? `, latest ${job.trades.length} shown` : ""}</h2>
          {job.trades.length === 0 ? <p className="msg">No trades yet.</p> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Date</th><th>Stock</th><th>Side</th><th className="num">Qty</th><th className="num">Price</th><th className="num">Amount</th><th className="num">Realized P&amp;L</th><th className="num">Cash after</th><th>Trigger / reason</th></tr></thead>
                <tbody>
                  {job.trades.map((t) => (
                    <tr key={t.trade_id} className={t.side === "BUY" ? "" : "row-hit"}>
                      <td>{t.trade_date}</td><td><strong>{t.symbol}</strong></td><td><span className={`rec-pill ${t.side === "BUY" ? "rec-buy" : "rec-sell"}`}>{t.side}</span></td>
                      <td className="num">{t.quantity.toLocaleString()}</td><td className="num">{money(t.price)}</td><td className="num">{money(t.amount)}</td>
                      <td className={`num ${tone(t.realized_pnl)}`}>{t.realized_pnl === null ? "" : signed(t.realized_pnl)}</td><td className="num">{money(t.cash_after, 0)}</td>
                      <td className="wrap">{t.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2 className="section-title">Agent runs — latest {job.assessments.length} of {Object.values(job.assessment_counts).reduce((a, b) => a + b, 0)}</h2>
          <p className="legend-note">mode: <em>run</em> = fresh model call · <em>rerun</em> = price near the cached buy price · <em>reassess</em> = held-stock refresh · <em>cached</em> = reused assessment · <em>skipped</em> = price far above the buy price, marked NOT BUY · <em>failed</em> = model call failed</p>
          {job.assessments.length === 0 ? <p className="msg">No agent runs yet.</p> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>Date</th><th>Stock</th><th>Agent</th><th>Phase</th><th>Mode</th><th>Verdict</th><th className="num">Price</th><th className="num">Buy at/below</th><th className="num">Stop</th><th className="num">Target</th><th>Buy?</th><th>Note</th></tr></thead>
                <tbody>
                  {job.assessments.map((a) => {
                    const href = decisionLink(a.skill_id, a.decision_id);
                    return (
                      <tr key={a.assessment_id} className={a.mode === "failed" ? "row-failed" : ""}>
                        <td>{a.sim_date}</td><td><strong>{a.symbol}</strong></td>
                        <td>{href ? <Link href={href}>{SKILL_NAME[a.skill_id] ?? a.skill_id}</Link> : SKILL_NAME[a.skill_id] ?? a.skill_id}</td>
                        <td>{a.phase}</td><td>{a.mode}</td><td><Pill rec={a.recommendation} /></td>
                        <td className="num">{money(a.current_price)}</td><td className="num">{money(a.buy_price)}</td><td className="num">{money(a.stop_price)}</td><td className="num">{money(a.target_price)}</td>
                        <td>{a.is_buy ? <strong style={{ color: "#0ca30c" }}>BUY</strong> : <span className="dim">not buy</span>}</td>
                        <td className="wrap">{a.note}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <p className="msg disclaimer">Every model-backed row links to its stored decision and reports. Simulated results on historical data; model-generated analysis, not investment advice.</p>
        </>
      )}
    </div>
  );
}
