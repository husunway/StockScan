"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { api, DiagnosticsPanel, LiveLog, type Diagnostics, type LogEntry } from "./RunUi";

type Day = { as_of_date: string; total: number; open: number; prescreen_rejected: number; murphy_buy: number; in_range: number; buffett_buy: number; all3: number; failed: number };
type Counts = { total: number; pending: number; running: number; failed: number; cancelled: number; prescreen_rejected: number; murphy_buy: number; in_range: number; buffett_buy: number; all3: number; ai_calls: number };
type Job = {
  job_id: number; created_at: string; status: "active" | "stopped" | "done"; start_date: string; end_date: string; cadence: string;
  options: Record<string, unknown>; backtests: Record<string, number>; log: LogEntry[]; counts: Counts; days: Day[];
};
type Item = {
  as_of_date: string; symbol: string; stage: string; status: string; reason: string | null; price: number | null; buy_below: number | null;
  stop_price: number | null; target_price: number | null; in_range: boolean | null; murphy_rec: string | null; buffett_rec: string | null;
  dimon_rec: string | null; murphy_decision_id: number | null; buffett_decision_id: number | null; dimon_decision_id: number | null;
  recommended_by: number; ai_calls: number; sector: string | null; updated_at: string | null;
};
type StepResult = { done: boolean; stopped?: boolean; item: Item | null; counts: Counts; log: LogEntry[] };

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const DOW = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const POLL_MS = 4000;
const iso = (y: number, m: number, d: number) => `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
const n = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(2));
const today = () => new Date().toISOString().slice(0, 10);

function monthCells(year: number, month: number): (number | null)[] {
  const offset = (new Date(year, month, 1).getDay() + 6) % 7;
  const days = new Date(year, month + 1, 0).getDate();
  return [...Array<null>(offset).fill(null), ...Array.from({ length: days }, (_, i) => i + 1)];
}

function Pill({ rec }: { rec: string | null }) {
  if (!rec) return <span className="tile-note">—</span>;
  return <span className={`rec-pill rec-${rec.toLowerCase()}`}>{rec}</span>;
}

export default function ScanPanel() {
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [diagError, setDiagError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [running, setRunning] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [startDate, setStartDate] = useState("2026-01-01");
  const [endDate, setEndDate] = useState(today());
  const [cadence, setCadence] = useState<"weekly" | "daily">("weekly");
  const [primaryUp, setPrimaryUp] = useState(true);
  const [rejectInterDown, setRejectInterDown] = useState(true);
  const [minAdx, setMinAdx] = useState<string>("");
  const [maxSymbols, setMaxSymbols] = useState<string>("");
  const [workers, setWorkers] = useState(1);
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [dayItems, setDayItems] = useState<Item[] | null>(null);
  const [showAllDay, setShowAllDay] = useState(false);
  const [hits, setHits] = useState<Item[]>([]);
  const [chart, setChart] = useState<{ symbol: string; date: string; murphy: string | null } | null>(null);
  const stopRef = useRef(false);
  const aborts = useRef<AbortController[]>([]);

  const note = useCallback((message: string, level: LogEntry["level"] = "info") => {
    setLog((prev) => [...prev, { t: new Date().toISOString(), elapsed_s: 0, level, message }]);
  }, []);

  const refreshJob = useCallback(async (id: number) => {
    try {
      const j = await api<Job>(`/api/py/scan/${id}`);
      setJob((prev) => (prev && prev.job_id !== id ? prev : j));
      return j;
    } catch {
      return null;
    }
  }, []);
  const refreshHits = useCallback(async (id: number) => {
    try {
      const h = await api<{ items: Item[] }>(`/api/py/scan/${id}/hits?min_stage=3`);
      setHits(h.items);
    } catch {}
  }, []);

  useEffect(() => {
    api<Diagnostics>("/api/py/agent/diagnostics").then(setDiag).catch((e: Error) => setDiagError(e.message));
    api<Job | null>("/api/py/scan/latest").then((j) => { if (j) { setJob(j); setLog(j.log); refreshHits(j.job_id); } }).catch(() => {});
  }, [refreshHits]);

  const active = !!job && job.status === "active" && job.counts.pending + job.counts.running > 0;
  useEffect(() => {
    if (!job || !active) return;
    const id = job.job_id;
    const t = setInterval(() => { refreshJob(id); refreshHits(id); }, POLL_MS);
    return () => clearInterval(t);
  }, [job?.job_id, active, refreshJob, refreshHits]); // eslint-disable-line react-hooks/exhaustive-deps

  const worker = useCallback(async (id: number, wid: number) => {
    for (;;) {
      if (stopRef.current) return;
      const ctrl = new AbortController();
      aborts.current.push(ctrl);
      try {
        const r = await api<StepResult>(`/api/py/scan/${id}/step`, { method: "POST", signal: ctrl.signal });
        setLog((prev) => [...prev, ...r.log].slice(-600));
        setJob((prev) => (prev && prev.job_id === id ? { ...prev, counts: r.counts, status: r.stopped ? "stopped" : r.done ? "done" : prev.status } : prev));
        if (r.item?.recommended_by === 3) refreshHits(id);
        if (r.done || r.stopped || !r.item) return;
      } catch (e) {
        if (e instanceof DOMException && e.name === "AbortError") return;
        note(`worker ${wid}: ${e instanceof Error ? e.message : "step failed"}`, "error");
        await new Promise((res) => setTimeout(res, 3000)); // transient error: back off, keep going
      } finally {
        aborts.current = aborts.current.filter((c) => c !== ctrl);
      }
    }
  }, [note, refreshHits]);

  const runWorkers = useCallback(async (id: number) => {
    stopRef.current = false;
    setRunning(true);
    setError(null);
    try {
      await Promise.all(Array.from({ length: workers }, (_, i) => worker(id, i + 1)));
    } finally {
      setRunning(false);
      refreshJob(id);
      refreshHits(id);
    }
  }, [workers, worker, refreshJob, refreshHits]);

  async function create() {
    setCreating(true);
    setError(null);
    setLog([]);
    setSelectedDay(null);
    setHits([]);
    note(`Creating scan: ${cadence} dates ${startDate} → ${endDate}; pre-screening every constituent from the TECH_ tables ...`);
    try {
      const j = await api<Job>("/api/py/scan", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ start_date: startDate, end_date: endDate, cadence, require_primary_up: primaryUp, reject_intermediate_down: rejectInterDown,
          min_adx: minAdx ? Number(minAdx) : null, max_symbols: maxSymbols ? Number(maxSymbols) : null }),
      });
      setJob(j);
      setLog((prev) => [...prev, ...j.log]);
      setCreating(false);
      note(`Scan #${j.job_id} created: ${j.counts.pending.toLocaleString()} model calls at stage 1 (John Murphy). Starting ${workers} worker(s).`);
      await runWorkers(j.job_id);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Could not create the scan";
      setError(msg);
      note(msg, "error");
      setCreating(false);
    }
  }

  async function stop() {
    if (!job) return;
    stopRef.current = true;
    aborts.current.forEach((c) => c.abort());
    try {
      const j = await api<Job>(`/api/py/scan/${job.job_id}/stop`, { method: "POST" });
      setJob(j);
      note(`Stopped: ${j.counts.cancelled} pending pairs cancelled`, "warn");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not stop");
    }
  }
  async function resume(retryFailed = false) {
    if (!job) return;
    try {
      const j = await api<Job>(`/api/py/scan/${job.job_id}/resume${retryFailed ? "?retry_failed=true" : ""}`, { method: "POST" });
      setJob(j);
      await runWorkers(j.job_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not resume");
    }
  }

  async function openDay(d: string) {
    if (!job) return;
    setSelectedDay(d);
    setDayItems(null);
    try {
      const r = await api<{ items: Item[] }>(`/api/py/scan/${job.job_id}/day/${d}${showAllDay ? "?all_items=true" : ""}`);
      setDayItems(r.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the day");
    }
  }
  useEffect(() => { if (selectedDay) openDay(selectedDay); }, [showAllDay]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!selectedDay || !job || !active) return;
    const t = setInterval(() => openDay(selectedDay), POLL_MS);
    return () => clearInterval(t);
  }, [selectedDay, job?.job_id, active]); // eslint-disable-line react-hooks/exhaustive-deps

  const dayMap = useMemo(() => new Map((job?.days ?? []).map((d) => [d.as_of_date, d])), [job]);
  const c = job?.counts;
  const processed = c ? c.total - c.pending - c.running : 0;
  const progress = c && c.total ? (processed / c.total) * 100 : 0;
  const decisionLink = (skill: string, id: number | null) => (job && id ? `/agent/backtest/decision?bt=${job.backtests[skill]}&id=${id}` : null);
  const year = Number(startDate.slice(0, 4)) || 2026;
  const months = Array.from({ length: 12 }, (_, m) => m).filter((m) => {
    const first = iso(year, m, 1), last = iso(year, m, new Date(year, m + 1, 0).getDate());
    return job ? last >= job.start_date && first <= job.end_date : last >= startDate && first <= endDate;
  });

  return (
    <div className="single" style={{ maxWidth: 1200 }}>
      <DiagnosticsPanel diag={diag} error={diagError} onLog={(entries) => setLog((prev) => [...prev, ...entries])} />

      <form className="filters agent-form" onSubmit={(e) => { e.preventDefault(); create(); }}>
        <label>From<input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} disabled={running} /></label>
        <label>To<input type="date" value={endDate} max={today()} onChange={(e) => setEndDate(e.target.value)} disabled={running} /></label>
        <label>Dates
          <select value={cadence} onChange={(e) => setCadence(e.target.value as "weekly" | "daily")} disabled={running}>
            <option value="weekly">every Monday</option>
            <option value="daily">every weekday (much larger)</option>
          </select>
        </label>
        <label>Min ADX (optional)<input type="number" value={minAdx} onChange={(e) => setMinAdx(e.target.value)} placeholder="e.g. 20" min={0} max={100} disabled={running} style={{ minWidth: 90 }} /></label>
        <label>Max stocks (optional)<input type="number" value={maxSymbols} onChange={(e) => setMaxSymbols(e.target.value)} placeholder="all" min={1} max={600} disabled={running} style={{ minWidth: 90 }} /></label>
        <label>Workers
          <select value={workers} onChange={(e) => setWorkers(Number(e.target.value))} disabled={running} style={{ minWidth: 70 }}>
            {[1, 2, 3, 4].map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
        <button type="submit" className="btn" disabled={creating || running}>{creating ? "Pre-screening…" : "Start scan"}</button>
        <button type="button" className="btn small danger" disabled={!(running || creating || active)} onClick={stop}>Stop</button>
        {job && !running && (job.counts.pending > 0 || job.counts.cancelled > 0) && <button type="button" className="btn small" onClick={() => resume(false)}>Resume</button>}
        {job && !running && job.counts.failed > 0 && <button type="button" className="btn small ghost" onClick={() => resume(true)}>Retry failed</button>}
      </form>
      <div className="toggles" style={{ marginTop: 0 }}>
        <label className="toggle"><input type="checkbox" checked={primaryUp} onChange={(e) => setPrimaryUp(e.target.checked)} disabled={running} /> pre-screen: primary trend up</label>
        <label className="toggle"><input type="checkbox" checked={rejectInterDown} onChange={(e) => setRejectInterDown(e.target.checked)} disabled={running} /> pre-screen: intermediate trend not down</label>
        <span className="legend-note">Funnel: pre-screen (free) → John Murphy → BUY with the close inside the entry zone → Warren Buffett → BUY → Jamie Dimon → BUY = all 3</span>
      </div>
      {error && <p className="msg error">{error}</p>}

      {job && c && (
        <>
          <div className="progress-row">
            <div className="progress"><div className="progress-bar" style={{ width: `${progress}%` }} /></div>
            <span className="progress-text">
              Scan #{job.job_id} · {processed.toLocaleString()} of {c.total.toLocaleString()} pairs settled · {c.ai_calls.toLocaleString()} model calls so far
              {c.pending + c.running > 0 && <> · {(c.pending + c.running).toLocaleString()} open</>}
              {job.status === "stopped" && <span className="stopped-chip">stopped</span>}
              {job.status === "done" && <span className="stopped-chip" style={{ background: "#0ca30c" }}>complete</span>}
              {(running || active) && job.status === "active" && <span className="spinner inline" aria-hidden />}
            </span>
          </div>
          <div className="tiles">
            <div className="tile tone-neutral"><span className="stat-label">Pre-screen rejected (free)</span><span className="tile-value">{c.prescreen_rejected.toLocaleString()}</span><span className="tile-note">never sent to a model</span></div>
            <div className="tile tone-neutral"><span className="stat-label">Murphy BUY</span><span className="tile-value">{c.murphy_buy.toLocaleString()}</span><span className="tile-note">{c.in_range.toLocaleString()} inside the entry zone</span></div>
            <div className="tile tone-neutral"><span className="stat-label">Buffett BUY</span><span className="tile-value">{c.buffett_buy.toLocaleString()}</span><span className="tile-note">of those sent to Buffett</span></div>
            <div className="tile tone-good"><span className="stat-label">Recommended by all 3</span><span className="tile-value">{c.all3.toLocaleString()}</span><span className="tile-note">Murphy + Buffett + Dimon</span></div>
            <div className={`tile ${c.failed ? "tone-bad" : "tone-neutral"}`}><span className="stat-label">Failed</span><span className="tile-value">{c.failed}</span><span className="tile-note">retry from the button above</span></div>
          </div>
        </>
      )}

      <LiveLog title={job ? `Scan log - #${job.job_id}` : "Log"} entries={log} running={running || creating || active} waitHint="waiting for the next model call (30-90 s each)" />

      {job && (
        <>
          <h2 className="section-title">Progress by date — stocks analyzed vs total</h2>
          <div className="day-progress">
            {job.days.map((d) => {
              const done = d.total - d.open;
              const p = d.total ? (done / d.total) * 100 : 0;
              return (
                <button type="button" key={d.as_of_date} className={`day-row ${selectedDay === d.as_of_date ? "sel" : ""}`} onClick={() => openDay(d.as_of_date)} title={`${d.as_of_date}: ${done} of ${d.total} settled · ${d.prescreen_rejected} pre-screened out · Murphy BUY ${d.murphy_buy} · Buffett BUY ${d.buffett_buy} · all 3: ${d.all3}`}>
                  <span className="day-date">{d.as_of_date}</span>
                  <span className="progress"><span className="progress-bar" style={{ width: `${p}%`, background: d.open > 0 ? "var(--accent)" : "#0ca30c" }} /></span>
                  <span className="day-count">{done.toLocaleString()} / {d.total.toLocaleString()}</span>
                  <span className={`day-hits ${d.all3 > 0 ? "has" : ""}`}>{d.all3 > 0 ? `★ ${d.all3}` : d.open > 0 ? "…" : "0"}</span>
                </button>
              );
            })}
          </div>

          <h2 className="section-title">Buy recommendations by date</h2>
          <div className="cal-legend">
            <span><i className="cal-key" /> not scanned</span><span><i className="cal-key sched" /> scanning / open</span>
            <span><i className="cal-key scan-0" /> scanned, no hit</span><span><i className="cal-key done" /> hits (number = stocks recommended by all 3)</span>
          </div>
          <div className="calendar">
            {months.map((m) => (
              <div key={m} className="cal-month">
                <h3>{MONTHS[m]} {year}</h3>
                <div className="cal-grid">
                  {DOW.map((d) => <span key={d} className="cal-dow">{d}</span>)}
                  {monthCells(year, m).map((day, i) => {
                    if (day === null) return <span key={`e${i}`} className="cal-day empty" />;
                    const date = iso(year, m, day);
                    const d = dayMap.get(date);
                    const cls = !d ? "" : d.all3 > 0 ? "done" : d.open > 0 ? "sched" : "scan-0";
                    const title = d ? `${date}: ${d.all3} recommended by all 3 · Murphy BUY ${d.murphy_buy} (${d.in_range} in zone) · Buffett BUY ${d.buffett_buy} · ${d.prescreen_rejected} pre-screened out · ${d.open} open${d.failed ? ` · ${d.failed} failed` : ""}` : date;
                    return (
                      <span key={date} className={`cal-day ${cls} ${selectedDay === date ? "cal-sel" : ""}`} title={title}>
                        {d ? <button type="button" className="cal-btn" onClick={() => openDay(date)}>{d.all3 > 0 ? d.all3 : day}</button> : day}
                      </span>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>

          {selectedDay && (
            <div className="card" style={{ marginTop: 12 }}>
              <div className="filters" style={{ marginBottom: 6 }}>
                <strong>{selectedDay}</strong>
                <label className="toggle"><input type="checkbox" checked={showAllDay} onChange={(e) => setShowAllDay(e.target.checked)} /> include pre-screen rejects</label>
                <button type="button" className="linklike" onClick={() => setSelectedDay(null)}>close</button>
              </div>
              {!dayItems ? <p className="msg">Loading…</p> : dayItems.length === 0 ? <p className="msg">Nothing reached a model on this date yet.</p> : (
                <div className="table-wrap">
                  <table className="table">
                    <thead><tr><th></th><th>Stock</th><th>Sector</th><th>Status</th><th className="num">Close</th><th className="num">Buy below</th><th className="num">Stop</th><th className="num">Target</th><th>Murphy</th><th>Buffett</th><th>Dimon</th><th>Result / reason</th></tr></thead>
                    <tbody>
                      {dayItems.map((it) => (
                        <tr key={it.symbol} className={`${it.recommended_by === 3 ? "row-hit" : ""} ${chart?.symbol === it.symbol && chart?.date === it.as_of_date ? "row-open" : ""}`}>
                          <td><button type="button" className="btn small ghost" onClick={() => setChart({ symbol: it.symbol, date: it.as_of_date, murphy: decisionLink("john-murphy", it.murphy_decision_id) })}>Chart</button></td>
                          <td><strong>{it.symbol}</strong></td><td>{it.sector ?? ""}</td>
                          <td>{it.status}{it.status === "pending" || it.status === "running" ? ` (${it.stage})` : ""}</td>
                          <td className="num">{n(it.price)}</td><td className="num">{n(it.buy_below)}</td><td className="num">{n(it.stop_price)}</td><td className="num">{n(it.target_price)}</td>
                          <td>{decisionLink("john-murphy", it.murphy_decision_id) ? <Link href={decisionLink("john-murphy", it.murphy_decision_id)!}><Pill rec={it.murphy_rec} /></Link> : <Pill rec={it.murphy_rec} />}{it.in_range === false && it.murphy_rec === "BUY" && <span className="tile-note"> out of zone</span>}</td>
                          <td>{decisionLink("warren-buffett", it.buffett_decision_id) ? <Link href={decisionLink("warren-buffett", it.buffett_decision_id)!}><Pill rec={it.buffett_rec} /></Link> : <Pill rec={it.buffett_rec} />}</td>
                          <td>{decisionLink("jamie-dimon", it.dimon_decision_id) ? <Link href={decisionLink("jamie-dimon", it.dimon_decision_id)!}><Pill rec={it.dimon_rec} /></Link> : <Pill rec={it.dimon_rec} />}</td>
                          <td className="wrap">{it.recommended_by === 3 ? <strong style={{ color: "#0ca30c" }}>★ Recommended by all 3</strong> : it.reason ?? ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {chart && (
            <div className="card" style={{ marginTop: 12, padding: 0, overflow: "hidden" }}>
              <div className="filters" style={{ padding: "10px 12px", marginBottom: 0 }}>
                <strong>{chart.symbol}</strong> technical chart as of {chart.date} — K chart, SMA 20/50/200, Bollinger, volume, RSI, MACD, support / resistance / Fibonacci, signals, patterns (TECH_ tables, nothing after that date)
                <a href={`/api/py/tech/${chart.symbol}/chart.html?as_of=${chart.date}`} target="_blank" rel="noreferrer">open in a new tab</a>
                {chart.murphy && <Link href={chart.murphy}>Murphy report</Link>}
                <button type="button" className="linklike" onClick={() => setChart(null)}>close</button>
              </div>
              <iframe className="pdf-frame report-frame" style={{ borderRadius: 0, border: 0, borderTop: "1px solid var(--border)" }} src={`/api/py/tech/${chart.symbol}/chart.html?as_of=${chart.date}`} title={`${chart.symbol} chart ${chart.date}`} />
            </div>
          )}

          <h2 className="section-title">All hits (recommended by all three) — {hits.length}</h2>
          {hits.length === 0 ? <p className="msg">None yet.</p> : (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th></th><th>Date</th><th>Stock</th><th>Sector</th><th className="num">Close</th><th className="num">Buy below</th><th className="num">Stop</th><th className="num">Target (Murphy)</th><th>Reports</th></tr></thead>
                <tbody>
                  {hits.map((it) => (
                    <tr key={`${it.as_of_date}-${it.symbol}`} className="row-hit">
                      <td><button type="button" className="btn small ghost" onClick={() => setChart({ symbol: it.symbol, date: it.as_of_date, murphy: decisionLink("john-murphy", it.murphy_decision_id) })}>Chart</button></td>
                      <td>{it.as_of_date}</td><td><strong>{it.symbol}</strong></td><td>{it.sector ?? ""}</td>
                      <td className="num">{n(it.price)}</td><td className="num">{n(it.buy_below)}</td><td className="num">{n(it.stop_price)}</td><td className="num">{n(it.target_price)}</td>
                      <td>
                        {(["john-murphy", "warren-buffett", "jamie-dimon"] as const).map((sk, i) => {
                          const id = [it.murphy_decision_id, it.buffett_decision_id, it.dimon_decision_id][i];
                          const href = decisionLink(sk, id);
                          return href ? <span key={sk}>{i > 0 && " · "}<Link href={href}>{["Murphy", "Buffett", "Dimon"][i]}</Link></span> : null;
                        })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="msg disclaimer">Each stage is a stored decision with its reports. Model-generated analysis; not investment advice.</p>
        </>
      )}
    </div>
  );
}
