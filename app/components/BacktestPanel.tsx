"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

type Skill = { id: string; name: string; description: string; version: string };
type Counts = { pending: number; running: number; done: number; failed: number; cancelled: number; total: number };
type LogEntry = { t: string; elapsed_s: number; level: "info" | "warn" | "error"; message: string };
type Diagnostics = {
  claude: { provider: string; endpoint: string | null; model: string | null; effort?: string; credentials: string | null; variables: string[] };
  database: {
    server: string | null; database: string | null; user: string | null; variables: string[];
    connected?: boolean; latency_ms?: number; version?: string; symbols_loaded?: number; price_rows?: number; error?: string;
  };
  blob: { configured: boolean; access: string; variables: string[] };
  skills_dir: string;
  skills: string[];
  vercel: boolean;
  python: string;
};
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

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const DOW = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const WEEK_OPTIONS = Array.from({ length: 52 }, (_, i) => i + 1);
const POLL_MS = 4000;

const n = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`);
const iso = (y: number, m: number, d: number) => `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`);
  return data as T;
}

function DiagnosticsPanel({ diag, error }: { diag: Diagnostics | null; error: string | null }) {
  const [open, setOpen] = useState(true);
  if (error) return <p className="msg error">Diagnostics unavailable: {error}</p>;
  if (!diag) return <p className="msg">Checking configuration…</p>;
  const db = diag.database;
  const dot = (ok: boolean | undefined) => <span className={`dot ${ok === undefined ? "" : ok ? "ok" : "down"}`} />;
  return (
    <div className="diag">
      <button type="button" className="linklike diag-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} Configuration used for this run
      </button>
      {open && (
        <div className="diag-grid">
          <div className="diag-item">
            <span className="stat-label">{dot(diag.claude.provider !== "not configured")} Claude</span>
            <strong>{diag.claude.provider}</strong>
            <span>{diag.claude.endpoint ?? "—"}</span>
            <span>model <code>{diag.claude.model ?? "—"}</code>{diag.claude.effort && <> · effort {diag.claude.effort}</>}</span>
            <span className="tile-note">{diag.claude.variables.join(" · ")}</span>
          </div>
          <div className="diag-item">
            <span className="stat-label">{dot(db.connected)} Azure SQL</span>
            <strong>{db.connected ? `connected in ${db.latency_ms} ms` : db.connected === false ? "connection failed" : "—"}</strong>
            <span>{db.server ?? "—"} / {db.database ?? "—"} as {db.user ?? "—"}</span>
            <span>{db.connected ? `${db.symbols_loaded} symbols, ${db.price_rows?.toLocaleString()} price rows` : db.error ?? ""}</span>
            <span className="tile-note">{db.variables.join(" · ")}</span>
          </div>
          <div className="diag-item">
            <span className="stat-label">{dot(diag.blob.configured)} Vercel Blob</span>
            <strong>{diag.blob.configured ? `configured (${diag.blob.access})` : "not configured"}</strong>
            <span>{diag.blob.configured ? "PDF reports will be stored" : "PDFs generated on demand"}</span>
            <span className="tile-note">{diag.blob.variables.join(" · ")}</span>
          </div>
          <div className="diag-item">
            <span className="stat-label">{dot(diag.skills.length > 0)} Runtime</span>
            <strong>{diag.vercel ? "Vercel function" : "local"} · Python {diag.python}</strong>
            <span>skills: {diag.skills.join(", ") || "none found"}</span>
            <span className="tile-note">{diag.skills_dir}</span>
          </div>
        </div>
      )}
    </div>
  );
}

function LiveLog({ title, entries, running }: { title: string; entries: LogEntry[]; running: boolean }) {
  const endRef = useRef<HTMLDivElement>(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [entries.length]);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  const last = entries[entries.length - 1];
  const waiting = running && last ? Math.max(0, Math.round((now - new Date(last.t).getTime()) / 1000)) : 0;
  return (
    <div className="console">
      <div className="console-title">
        {title}
        {running && <span className="spinner inline" aria-hidden />}
      </div>
      <div className="console-body">
        {entries.length === 0 && <div className="console-line muted">No log yet.</div>}
        {entries.map((e, i) => (
          <div key={i} className={`console-line ${e.level}`}>
            <span className="console-time">{e.t.slice(11, 19)}</span>
            <span className="console-elapsed">+{e.elapsed_s.toFixed(1)}s</span>
            <span>{e.message}</span>
          </div>
        ))}
        {running && last && (
          <div className={`console-line ${waiting > 150 ? "warn" : "muted"}`}>
            <span className="console-time">…</span>
            <span className="console-elapsed">{waiting}s</span>
            <span>
              {waiting > 150
                ? "No update for over 150 s. The server function may have been terminated (Vercel limit 120 s per step). Use Stop run, then Retry failed; consider ANTHROPIC_EFFORT=medium."
                : "waiting for the next update (progress lines appear every ~15 s while the model generates)"}
            </span>
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}

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
        <span><i className="cal-key done" /> completed — click for the report</span>
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
                return (
                  <span key={date} className={`cal-day ${cls}`} title={title}>
                    {clickable ? (
                      <Link href={`/agent/backtest/decision?bt=${bt.backtest_id}&id=${dec.decision_id}`}>{day}</Link>
                    ) : (
                      day
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
  const [symbol, setSymbol] = useState(params.get("symbol")?.toUpperCase() ?? "");
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
  const [preparing, setPreparing] = useState(false);
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

  const loadPrevious = useCallback(async (sym: string, sk: string) => {
    if (!sym || !sk) return;
    try {
      const d = await api<{ backtests: Previous[] }>(`/api/py/agent/backtests?symbol=${encodeURIComponent(sym)}&skill=${sk}`);
      setPrevious(d.backtests);
    } catch {
      setPrevious([]);
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(() => {
      loadPrevious(symbol.trim(), skillId);
      loadDataStatus(symbol.trim());
    }, 400);
    return () => clearTimeout(t);
  }, [symbol, skillId, loadPrevious, loadDataStatus]);

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
      <DiagnosticsPanel diag={diag} error={diagError} />
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
      </form>
      <p className="msg hint">
        {skill ? <>{skill.name}: </> : null}
        for each Monday the agent sees only prices before that date and financial reports already published by then.
        Every report and data set it consumed is listed with the decision, and a PDF report is stored for each run.
        Each Monday is one model call (30–90 s); you can stop and resume.
      </p>

      {symbol.trim() && (
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
        <LiveLog title={preparing ? "Downloading data" : "Preparing"} entries={clientLog} running={creating || preparing} />
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
