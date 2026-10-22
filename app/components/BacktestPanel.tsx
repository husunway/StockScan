"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api, DiagnosticsPanel, LiveLog, type Diagnostics, type LogEntry } from "./RunUi";

type Skill = { id: string; name: string; description: string; version: string; data_source?: string };
type TechStatus = {
  symbol: string;
  available: boolean;
  fresh?: boolean;
  computed_at?: string;
  first_trade_date?: string;
  last_trade_date?: string;
  latest_price_date?: string | null;
  price_rows?: number;
  counts?: Record<string, number>;
  tables?: string[];
  log?: LogEntry[];
};
type Counts = { pending: number; running: number; done: number; failed: number; cancelled: number; total: number };
type Decision = {
  decision_id: number;
  as_of_date: string;
  status: "pending" | "running" | "done" | "failed" | "cancelled";
  log: LogEntry[];
  recommendation: "BUY" | "HOLD" | "SELL" | null;
  conviction: string | null;
  current_price: number | null;
  buy_below_price: number | null;
  target_price: number | null;
  stop_price: number | null;
  intrinsic_value_base: number | null;
  margin_of_safety_pct: number | null;
  headline: string | null;
  error: string | null;
  report_stored: boolean;
};
type Backtest = {
  backtest_id: number;
  skill_id: string;
  symbol: string;
  year: number;
  created_at: string;
  status: "active" | "stopped";
  counts: Counts;
  log: LogEntry[];
  decisions: Decision[];
};
type Previous = { backtest_id: number; skill_id: string; symbol: string; year: number; created_at: string; done: number; failed: number; total: number };
type DataStatus = {
  symbol: string;
  available: boolean;
  fresh?: boolean;
  age_hours?: number;
  fetched_at?: string;
  annual_periods?: string[];
  quarterly_periods?: string[];
  annual_line_items?: number;
  quarterly_line_items?: number;
  earnings_dates?: number;
  quote_summary_fields?: number;
  price_rows?: number;
  price_range?: [string, string] | null;
  data_pathname?: string | null;
  data_files?: string[];
  log?: LogEntry[];
};

const LAST_SYMBOL_KEY = "stockscan.backtest.lastSymbol";
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const DOW = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const WEEK_OPTIONS = Array.from({ length: 52 }, (_, i) => i + 1);
const POLL_MS = 4000;

const n = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`);
const iso = (y: number, m: number, d: number) => `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;

/** Month grid starting on Monday; null cells pad the first week. */
function monthCells(year: number, month: number): (number | null)[] {
  const offset = (new Date(year, month, 1).getDay() + 6) % 7;
  const days = new Date(year, month + 1, 0).getDate();
  return [...Array<null>(offset).fill(null), ...Array.from({ length: days }, (_, i) => i + 1)];
}

function YearCalendar({ bt }: { bt: Backtest }) {
  const byDate = new Map(bt.decisions.map((d) => [d.as_of_date, d]));
  return (
    <>
      <div className="cal-legend">
        <span><i className="cal-key" /> no run</span>
        <span><i className="cal-key sched" /> scheduled</span>
        <span><i className="cal-key running" /> running</span>
        <span><i className="cal-key done" /> completed — shows the verdict (B buy · H hold · S sell); click for the report</span>
        <span><i className="cal-key failed" /> failed</span>
        <span><i className="cal-key cancelled" /> cancelled</span>
      </div>
      <div className="calendar">
        {MONTHS.map((name, m) => (
          <div key={name} className="cal-month">
            <h3>{name} {bt.year}</h3>
            <div className="cal-grid">
              {DOW.map((d) => <span key={d} className="cal-dow">{d}</span>)}
              {monthCells(bt.year, m).map((day, i) => {
                if (day === null) return <span key={`e${i}`} className="cal-day empty" />;
                const date = iso(bt.year, m, day);
                const dec = byDate.get(date);
                const cls = dec ? (dec.status === "pending" ? "sched" : dec.status) : "";
                const clickable = dec && (dec.status === "done" || dec.status === "failed");
                const title = dec
                  ? `${date}: ${dec.status}${dec.recommendation ? ` · ${dec.recommendation}` : ""}${dec.headline ? ` · ${dec.headline}` : ""}${dec.error ? ` · ${dec.error}` : ""}`
                  : date;
                // Completed Mondays show the verdict letter (B / H / S) instead of the day number.
                const label = dec?.status === "done" && dec.recommendation ? dec.recommendation[0] : String(day);
                return (
                  <span key={date} className={`cal-day ${cls}`} title={title}>
                    {clickable ? (
                      <Link href={`/agent/backtest/decision?bt=${bt.backtest_id}&id=${dec.decision_id}`}>{label}</Link>
                    ) : (
                      label
                    )}
                  </span>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

export default function BacktestPanel() {
  const params = useSearchParams();
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillId, setSkillId] = useState(params.get("skill") ?? "warren-buffett");
  const [symbol, setSymbol] = useState(() => {
    const fromUrl = params.get("symbol")?.toUpperCase();
    if (fromUrl) return fromUrl;
    try {
      return localStorage.getItem(LAST_SYMBOL_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [year, setYear] = useState(2026);
  const [weeks, setWeeks] = useState(10);
  const [known, setKnown] = useState<string[]>([]);
  const [previous, setPrevious] = useState<Previous[]>([]);
  const [bt, setBt] = useState<Backtest | null>(null);
  const [running, setRunning] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [diagError, setDiagError] = useState<string | null>(null);
  const [clientLog, setClientLog] = useState<LogEntry[]>([]);
  const [dataStatus, setDataStatus] = useState<DataStatus | null>(null);
  const [techStatus, setTechStatus] = useState<TechStatus | null>(null);
  const [preparing, setPreparing] = useState(false);

  const loadTechStatus = useCallback(async (sym: string) => {
    if (!sym) return setTechStatus(null);
    try {
      setTechStatus(await api<TechStatus>(`/api/py/tech/${encodeURIComponent(sym)}`));
    } catch {
      setTechStatus(null);
    }
  }, []);

  async function prepareTech() {
    const s = symbol.trim();
    if (!s) return;
    setPreparing(true);
    setError(null);
    setClientLog([]);
    note(`Computing technical data for ${s}: indicators, trend, pivots, levels, signals, patterns -> TECH_ tables ...`);
    try {
      const r = await api<TechStatus>("/api/py/tech/prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: s }),
      });
      for (const e of r.log ?? []) setClientLog((prev) => [...prev, e]);
      note("Technical data ready in the TECH_ tables");
      await loadTechStatus(s);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Technical data preparation failed";
      setError(msg);
      note(msg, "error");
    } finally {
      setPreparing(false);
    }
  }
  const stopRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const [stopping, setStopping] = useState(false);

  const loadDataStatus = useCallback(async (sym: string) => {
    if (!sym) return setDataStatus(null);
    try {
      setDataStatus(await api<DataStatus>(`/api/py/data/${encodeURIComponent(sym)}`));
    } catch {
      setDataStatus(null);
    }
  }, []);

  async function prepareData(refresh: boolean) {
    const s = symbol.trim();
    if (!s) return;
    setPreparing(true);
    setError(null);
    setClientLog([]);
    note(`Pre-downloading all documents and data for ${s} from Yahoo Finance (prices, annual and quarterly reports, earnings dates, profile, quote summary) ...`);
    try {
      const r = await api<DataStatus>("/api/py/data/prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: s, refresh }),
      });
      for (const e of r.log ?? []) setClientLog((prev) => [...prev, e]);
      note(`Data bundle ready${r.data_pathname ? ` - stored in Blob at ${r.data_pathname}` : ""}`);
      await loadDataStatus(s);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Data download failed";
      setError(msg);
      note(msg, "error");
    } finally {
      setPreparing(false);
    }
  }

  const note = useCallback((message: string, level: LogEntry["level"] = "info") => {
    setClientLog((prev) => [...prev, { t: new Date().toISOString(), elapsed_s: 0, level, message }]);
  }, []);

  useEffect(() => {
    api<Diagnostics>("/api/py/agent/diagnostics").then(setDiag).catch((e: Error) => setDiagError(e.message));
    api<{ skills: Skill[] }>("/api/py/agent/skills")
      .then((d) => {
        setSkills(d.skills);
        setSkillId((prev) => (d.skills.some((s) => s.id === prev) ? prev : d.skills[0]?.id ?? ""));
      })
      .catch((e: Error) => setError(e.message));
    api<{ symbols: { symbol: string }[] }>("/api/py/symbols")
      .then((d) => setKnown(d.symbols.map((s) => s.symbol)))
      .catch(() => {});
    const id = params.get("bt");
    if (id) openPrevious(Number(id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadPrevious = useCallback(async (sym: string, sk: string): Promise<Previous[]> => {
    if (!sym || !sk) return [];
    try {
      const d = await api<{ backtests: Previous[] }>(`/api/py/agent/backtests?symbol=${encodeURIComponent(sym)}&skill=${sk}`);
      setPrevious(d.backtests);
      return d.backtests;
    } catch {
      setPrevious([]);
      return [];
    }
  }, []);

  // Remember the symbol per browser, and keep the URL in sync so refresh / back restores the view.
  useEffect(() => {
    try {
      if (symbol.trim()) localStorage.setItem(LAST_SYMBOL_KEY, symbol.trim());
    } catch {}
  }, [symbol]);
  useEffect(() => {
    const q = new URLSearchParams();
    if (skillId) q.set("skill", skillId);
    if (symbol.trim()) q.set("symbol", symbol.trim());
    if (bt) q.set("bt", String(bt.backtest_id));
    window.history.replaceState(null, "", `${window.location.pathname}?${q}`);
  }, [skillId, symbol, bt?.backtest_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // When the symbol or skill changes, refresh the data-bundle status and the list of previous runs.
  // A run is only shown on request (Load last run / a run link / ?bt= in the URL), never automatically.
  useEffect(() => {
    const sym = symbol.trim();
    const t = setTimeout(() => {
      loadDataStatus(sym);
      loadTechStatus(sym);
      loadPrevious(sym, skillId);
    }, 400);
    return () => clearTimeout(t);
  }, [symbol, skillId, loadPrevious, loadDataStatus, loadTechStatus]);

  async function loadLastRun() {
    const sym = symbol.trim();
    if (!sym || !skillId) return;
    setError(null);
    const list = previous.length ? previous : await loadPrevious(sym, skillId);
    if (list.length === 0) {
      setError(`No previous run of ${skillId} for ${sym}.`);
      return;
    }
    await openPrevious(list[0].backtest_id);
  }

  // Live status: poll the backtest while any Monday is pending or running (even if another tab drives it).
  const active = !!bt && bt.counts.pending + bt.counts.running > 0;
  useEffect(() => {
    if (!bt || !active) return;
    const id = bt.backtest_id;
    const t = setInterval(() => {
      api<Backtest>(`/api/py/agent/backtest/${id}`).then((d) => setBt((prev) => (prev?.backtest_id === id ? d : prev))).catch(() => {});
    }, POLL_MS);
    return () => clearInterval(t);
  }, [bt?.backtest_id, active]); // eslint-disable-line react-hooks/exhaustive-deps

  const runSteps = useCallback(async (id: number, retryFailed = false) => {
    stopRef.current = false;
    setRunning(true);
    setError(null);
    try {
      for (;;) {
        if (stopRef.current) break;
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        const r = await api<{ done: boolean; stopped?: boolean; decision: Decision | null; counts: Counts }>(
          `/api/py/agent/backtest/${id}/step${retryFailed ? "?retry_failed=true" : ""}`,
          { method: "POST", signal: ctrl.signal }
        );
        abortRef.current = null;
        if (r.stopped) stopRef.current = true;
        setBt((prev) =>
          prev && prev.backtest_id === id
            ? {
                ...prev,
                counts: r.counts,
                decisions: r.decision
                  ? prev.decisions.map((d) => (d.decision_id === r.decision!.decision_id ? r.decision! : d))
                  : prev.decisions,
              }
            : prev
        );
        if (r.done || !r.decision) break;
        retryFailed = false;
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setError(e instanceof Error ? e.message : "Step failed");
      }
    } finally {
      abortRef.current = null;
      setRunning(false);
    }
  }, []);

  async function stopRun() {
    if (!bt) return;
    setStopping(true);
    stopRef.current = true;
    abortRef.current?.abort(); // drop the browser's wait on the current step
    try {
      const d = await api<Backtest>(`/api/py/agent/backtest/${bt.backtest_id}/stop`, { method: "POST" });
      setBt(d);
      note(`Run stopped: ${d.counts.cancelled} Monday(s) cancelled`, "warn");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not stop the run");
    } finally {
      setStopping(false);
    }
  }

  async function resumeRun(retryFailed = false) {
    if (!bt) return;
    try {
      const d = await api<Backtest>(`/api/py/agent/backtest/${bt.backtest_id}/resume`, { method: "POST" });
      setBt(d);
      await runSteps(d.backtest_id, retryFailed);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not resume the run");
    }
  }

  async function create(e: React.FormEvent) {
    e.preventDefault();
    const s = symbol.trim();
    if (!s || !skillId) return;
    setCreating(true);
    setError(null);
    setBt(null);
    setClientLog([]);
    note(`Preparing backtest: ${skillId} on ${s}, first ${weeks} Monday(s) of ${year} - checking stored prices and fetching statements from Yahoo Finance ...`);
    try {
      const created = await api<Backtest>("/api/py/agent/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill: skillId, symbol: s, year, weeks }),
      });
      setBt(created);
      setCreating(false);
      note(`Backtest #${created.backtest_id} created; starting the first Monday`);
      loadPrevious(s, skillId);
      await runSteps(created.backtest_id);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Could not create backtest";
      setError(msg);
      note(msg, "error");
      setCreating(false);
    }
  }

  async function openPrevious(id: number) {
    setError(null);
    try {
      const d = await api<Backtest>(`/api/py/agent/backtest/${id}`);
      setBt(d);
      setSymbol(d.symbol);
      setSkillId(d.skill_id);
      setYear(d.year);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load backtest");
    }
  }

  const skill = skills.find((s) => s.id === skillId);
  const c = bt?.counts;
  const done = bt?.decisions.filter((d) => d.status === "done") ?? [];
  const tally = { BUY: 0, HOLD: 0, SELL: 0 };
  for (const d of done) if (d.recommendation) tally[d.recommendation]++;
  const progress = c && c.total ? ((c.done + c.failed) / c.total) * 100 : 0;
  // The live log follows the Monday in progress, else the most recently finished one.
  const current =
    bt?.decisions.find((d) => d.status === "running") ??
    [...(bt?.decisions ?? [])].reverse().find((d) => d.status === "done" || d.status === "failed");

  return (
    <div className="single">
      <DiagnosticsPanel diag={diag} error={diagError} onLog={(entries) => setClientLog((prev) => [...prev, ...entries])} />
      <form className="filters agent-form" onSubmit={create}>
        <label>
          Skill
          <select value={skillId} onChange={(e) => setSkillId(e.target.value)} disabled={!skills.length || running}>
            {skills.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <label>
          Symbol
          <input list="known-symbols" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} placeholder="e.g. KO, AAPL, RY.TO" maxLength={20} disabled={running} />
          <datalist id="known-symbols">{known.map((s) => <option key={s} value={s} />)}</datalist>
        </label>
        <label>
          Year
          <input type="number" value={year} min={2000} max={2100} onChange={(e) => setYear(Number(e.target.value))} disabled={running} style={{ minWidth: 90 }} />
        </label>
        <label>
          Mondays to run (from Jan 1)
          <select value={weeks} onChange={(e) => setWeeks(Number(e.target.value))} disabled={running} style={{ minWidth: 90 }}>
            {WEEK_OPTIONS.map((w) => <option key={w} value={w}>{w}</option>)}
          </select>
        </label>
        <button type="submit" className="btn" disabled={creating || running || !symbol.trim() || !skillId}>
          {creating ? "Preparing…" : `Run first ${weeks} Monday${weeks === 1 ? "" : "s"} of ${year}`}
        </button>
        <button
          type="button"
          className="btn ghost"
          disabled={creating || running || !symbol.trim() || !skillId || previous.length === 0}
          title={previous.length ? `Load run #${previous[0].backtest_id} (${previous[0].year}, ${previous[0].done}/${previous[0].total} done)` : "No previous run for this symbol and skill"}
          onClick={loadLastRun}
        >
          Load last run
        </button>
      </form>
      <p className="msg hint">
        {skill ? <>{skill.name}: </> : null}
        for each Monday the agent sees only prices before that date and financial reports already published by then.
        Every report and data set it consumed is listed with the decision, and a PDF report is stored for each run.
        Each Monday is one model call (30–90 s); you can stop and resume.
      </p>

      {symbol.trim() && skill?.data_source === "tech" && (
        <div className="data-status">
          <span className="stat-label">Technical data for {symbol.trim()} (TECH_ tables)</span>
          {techStatus?.available ? (
            <span>
              <span className={`dot ${techStatus.fresh ? "ok" : "down"}`} /> computed {techStatus.computed_at?.slice(0, 16).replace("T", " ")} UTC ·
              {techStatus.price_rows} trading days {techStatus.first_trade_date} → {techStatus.last_trade_date}
              {!techStatus.fresh && techStatus.latest_price_date && <> · stale: prices now to {techStatus.latest_price_date}</>}
              {techStatus.counts && (
                <> · {Object.entries(techStatus.counts).map(([k, v]) => `${k.replace("TECH_", "").toLowerCase()} ${v}`).join(", ")}</>
              )}
            </span>
          ) : (
            <span><span className="dot" /> not computed yet - it will be computed when a run starts, or prepare now</span>
          )}
          <button type="button" className="btn small ghost" disabled={preparing || running} onClick={prepareTech}>
            {preparing ? "Computing…" : techStatus?.available ? "Recompute technical data" : "Prepare technical data"}
          </button>
        </div>
      )}

      {symbol.trim() && skill?.data_source !== "tech" && (
        <div className="data-status">
          <span className="stat-label">Data bundle for {symbol.trim()}</span>
          {dataStatus?.available ? (
            <span>
              <span className={`dot ${dataStatus.fresh ? "ok" : "down"}`} /> fetched {dataStatus.age_hours}h ago ·
              prices {dataStatus.price_range?.[0]} → {dataStatus.price_range?.[1]} ({dataStatus.price_rows} rows) ·
              annual {dataStatus.annual_periods?.map((p) => p.slice(0, 4)).join(", ")} · quarterly {dataStatus.quarterly_periods?.length} periods ·
              {dataStatus.annual_line_items} + {dataStatus.quarterly_line_items} line items · {dataStatus.earnings_dates} earnings dates ·
              {dataStatus.quote_summary_fields} quote fields
              {dataStatus.data_pathname ? (
                <>
                  {" · "}Blob <code>{dataStatus.data_pathname.replace(/latest\.json$/, "")}</code>
                  {dataStatus.data_files && dataStatus.data_files.length > 0 && (
                    <> ({dataStatus.data_files.length} files: profile, quote summary, earnings dates, prices, annual and quarterly statements, latest.json, snapshot, index.json)</>
                  )}
                </>
              ) : " · SQL only"}
            </span>
          ) : (
            <span><span className="dot" /> not downloaded yet - it will be fetched when a run starts, or pre-download now</span>
          )}
          <button type="button" className="btn small ghost" disabled={preparing || running} onClick={() => prepareData(!!dataStatus?.available)}>
            {preparing ? "Downloading…" : dataStatus?.available ? "Refresh data" : "Pre-download data"}
          </button>
        </div>
      )}

      {bt && !running && c && c.pending > 0 && c.running === 0 && bt.status !== "stopped" && (
        <p className="msg hint resume-hint">
          Run #{bt.backtest_id} has {c.pending} Monday{c.pending === 1 ? "" : "s"} left and nothing is driving it from
          this page (the run continues only while a tab is on it). Click <strong>Resume</strong> below to continue.
        </p>
      )}
      {bt && !running && c && c.running > 0 && (
        <p className="msg hint">Run #{bt.backtest_id} is in progress elsewhere; this page updates live.</p>
      )}

      {previous.length > 0 && !running && (
        <p className="msg hint">
          Previous runs for {symbol.trim()}:{" "}
          {previous.slice(0, 6).map((p, i) => (
            <span key={p.backtest_id}>
              {i > 0 && " · "}
              <button type="button" className="linklike" onClick={() => openPrevious(p.backtest_id)}>
                #{p.backtest_id} {p.year} ({p.done}/{p.total} done{p.failed ? `, ${p.failed} failed` : ""})
              </button>
            </span>
          ))}
        </p>
      )}

      {error && <p className="msg error">{error}</p>}

      {(creating || preparing || clientLog.length > 0) && !bt && (
        <LiveLog title={preparing ? "Downloading data" : creating ? "Preparing" : "Debug"} entries={clientLog} running={creating || preparing} />
      )}

      {bt && c && (
        <div className="buffett">
          <LiveLog
            title={current ? `Run log - Monday ${current.as_of_date} (${current.status})` : "Run log"}
            entries={[...(bt.log ?? []), ...(current?.log ?? [])]}
            running={running || active}
          />
          <div className="progress-row">
            <div className="progress"><div className="progress-bar" style={{ width: `${progress}%` }} /></div>
            <span className="progress-text">
              #{bt.backtest_id} {bt.symbol} · {c.done} done · {c.failed} failed · {c.pending + c.running} remaining
              {c.cancelled > 0 && <> · {c.cancelled} cancelled</>} of {c.total}
              {bt.status === "stopped" && <span className="stopped-chip">stopped</span>}
              {(running || active) && bt.status !== "stopped" && <span className="spinner inline" aria-hidden />}
            </span>
            {bt.status !== "stopped" && (running || active) && (
              <button type="button" className="btn small danger" disabled={stopping} onClick={stopRun}>
                {stopping ? "Stopping…" : "Stop run"}
              </button>
            )}
            {running && (
              <button type="button" className="btn small ghost" onClick={() => { stopRef.current = true; }}>Stop after current</button>
            )}
            {!running && (bt.status === "stopped" || c.pending > 0) && (c.pending + c.cancelled > 0) && (
              <button type="button" className="btn small" onClick={() => resumeRun()}>Resume</button>
            )}
            {!running && c.failed > 0 && (
              <button type="button" className="btn small ghost" onClick={() => resumeRun(true)}>Retry failed</button>
            )}
          </div>

          <div className="tiles">
            {(["BUY", "HOLD", "SELL"] as const).map((r) => (
              <div key={r} className={`tile tone-${r === "BUY" ? "good" : r === "SELL" ? "bad" : "neutral"}`}>
                <span className="stat-label">{r}</span>
                <span className="tile-value">{tally[r]}</span>
                <span className="tile-note">of {done.length} completed Mondays</span>
              </div>
            ))}
          </div>

          <YearCalendar bt={bt} />

          <h2 className="section-title">Decisions</h2>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Monday</th><th>Status</th><th>Verdict</th><th>Conviction</th>
                  <th className="num">Price</th><th className="num">Buy below</th><th className="num">Target</th>
                  <th className="num">Stop</th><th className="num">IV base</th><th className="num">MoS</th><th>Headline</th><th>Report</th>
                </tr>
              </thead>
              <tbody>
                {bt.decisions.map((d) => (
                  <tr key={d.decision_id} className={`row-${d.status}`}>
                    <td>{d.as_of_date}</td>
                    <td>{d.status === "running" ? "running…" : d.status}</td>
                    <td>{d.recommendation ? <span className={`rec-pill rec-${d.recommendation.toLowerCase()}`}>{d.recommendation}</span> : "—"}</td>
                    <td>{d.conviction ?? "—"}</td>
                    <td className="num">{n(d.current_price)}</td>
                    <td className="num">{n(d.buy_below_price)}</td>
                    <td className="num">{n(d.target_price)}</td>
                    <td className="num">{n(d.stop_price)}</td>
                    <td className="num">{n(d.intrinsic_value_base)}</td>
                    <td className="num">{pct(d.margin_of_safety_pct)}</td>
                    <td className="wrap">{d.error ? <span className="verdict-fail">{d.error}</span> : d.headline ?? ""}</td>
                    <td>
                      {d.status === "done" || d.status === "failed" ? (
                        <Link href={`/agent/backtest/decision?bt=${bt.backtest_id}&id=${d.decision_id}`}>open</Link>
                      ) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="msg disclaimer">
            Point-in-time data is reconstructed from reported statements and stored prices; the model&apos;s own training
            knowledge cannot be rewound, so treat results as a test of the framework on the data, not as hindsight-free.
            Model-generated analysis; not investment advice.
          </p>
        </div>
      )}
    </div>
  );
}
