"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, DiagnosticsPanel, LiveLog, type Diagnostics, type LogEntry } from "./RunUi";

type Constituent = {
  symbol: string;
  name: string | null;
  sector: string | null;
  sub_industry: string | null;
  price_rows: number;
  price_first: string | null;
  price_last: string | null;
  tech_computed_at: string | null;
  tech_last: string | null;
  tech_fresh: boolean;
  state: "ready" | "stale" | "missing";
};
type Universe = {
  items: Constituent[];
  summary: { total: number; ready: number; stale: number; missing: number };
  list_updated_at: string | null;
  list_source: string | null;
  latest_job: { job_id: number; status: string; created_at: string } | null;
};
type Item = {
  symbol: string;
  status: "pending" | "running" | "done" | "skipped" | "failed" | "cancelled";
  price_action: string | null;
  price_rows_added: number | null;
  price_last_date: string | null;
  tech_action: string | null;
  tech_last_date: string | null;
  message: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
};
type Counts = { pending: number; running: number; done: number; skipped: number; failed: number; cancelled: number; total: number };
type Job = { job_id: number; created_at: string; status: "active" | "stopped" | "done"; options: { batch_size?: number; only_stale?: boolean }; log: LogEntry[]; counts: Counts; items: Item[] };
type StepResult = { done: boolean; stopped?: boolean; processed: Item[]; counts: Counts; log: LogEntry[] };

const POLL_MS = 3000;
const when = (iso: string | null) => (iso ? new Date(iso.endsWith("Z") ? iso : iso + "Z").toLocaleString() : "—");

export default function UniverseLoader() {
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [diagError, setDiagError] = useState<string | null>(null);
  const [universe, setUniverse] = useState<Universe | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [batchSize, setBatchSize] = useState(5);
  const [onlyStale, setOnlyStale] = useState(true);
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selected, setSelected] = useState<string | null>(null);
  const stopRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const batchTimes = useRef<number[]>([]);

  const note = useCallback((message: string, level: LogEntry["level"] = "info") => {
    setLog((prev) => [...prev, { t: new Date().toISOString(), elapsed_s: 0, level, message }]);
  }, []);

  const loadUniverse = useCallback(async () => {
    try {
      const u = await api<Universe>("/api/py/universe");
      setUniverse(u);
      return u;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the universe");
      return null;
    }
  }, []);

  useEffect(() => {
    api<Diagnostics>("/api/py/agent/diagnostics").then(setDiag).catch((e: Error) => setDiagError(e.message));
    loadUniverse();
    api<Job | null>("/api/py/universe/load/latest")
      .then((j) => {
        if (j) {
          setJob(j);
          setLog(j.log);
        }
      })
      .catch(() => {});
  }, [loadUniverse]);

  // Live status while a job is active (also when another tab drives it).
  const active = !!job && job.status === "active" && job.counts.pending + job.counts.running > 0;
  useEffect(() => {
    if (!job || !active) return;
    const id = job.job_id;
    const t = setInterval(() => {
      api<Job>(`/api/py/universe/load/${id}`).then((j) => setJob((prev) => (prev?.job_id === id ? j : prev))).catch(() => {});
    }, POLL_MS);
    return () => clearInterval(t);
  }, [job?.job_id, active]); // eslint-disable-line react-hooks/exhaustive-deps

  const runSteps = useCallback(async (id: number) => {
    stopRef.current = false;
    setRunning(true);
    setError(null);
    let steps = 0;
    try {
      for (;;) {
        if (stopRef.current) break;
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        const t0 = Date.now();
        const r = await api<StepResult>(`/api/py/universe/load/${id}/step`, { method: "POST", signal: ctrl.signal });
        abortRef.current = null;
        batchTimes.current = [...batchTimes.current.slice(-9), Date.now() - t0];
        setLog((prev) => [...prev, ...r.log].slice(-500));
        setJob((prev) =>
          prev && prev.job_id === id
            ? { ...prev, counts: r.counts, status: r.stopped ? "stopped" : r.done ? "done" : prev.status, items: prev.items.map((it) => r.processed.find((p) => p.symbol === it.symbol) ?? it) }
            : prev
        );
        steps++;
        if (steps % 5 === 0) loadUniverse();
        if (r.done || r.stopped) break;
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) setError(e instanceof Error ? e.message : "Step failed");
    } finally {
      abortRef.current = null;
      setRunning(false);
      loadUniverse();
    }
  }, [loadUniverse]);

  async function startLoad() {
    stopRef.current = false;
    setBusy("start");
    setError(null);
    setLog([]);
    note(`Creating load job: ${onlyStale ? "only symbols with missing or stale data" : "all constituents"}, ${batchSize} per batch ...`);
    try {
      const j = await api<Job>("/api/py/universe/load", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ only_stale: onlyStale, batch_size: batchSize }),
      });
      setJob(j);
      setLog((prev) => [...prev, ...j.log]);
      setBusy(null);
      if (stopRef.current) {
        const stopped = await api<Job>(`/api/py/universe/load/${j.job_id}/stop`, { method: "POST" });
        setJob(stopped);
        note("Stopped before the first batch", "warn");
        return;
      }
      await runSteps(j.job_id);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Could not start";
      setError(msg);
      note(msg, "error");
      setBusy(null);
    }
  }

  const canStop = running || busy === "start" || (!!job && job.status === "active" && job.counts.pending + job.counts.running > 0);

  async function stopLoad() {
    stopRef.current = true;          // stops the step loop (and prevents one from starting after a create)
    abortRef.current?.abort();       // drops the browser's wait on the current batch
    if (!job) {
      note("Stop requested before the job was created; it will not start", "warn");
      return;
    }
    try {
      const j = await api<Job>(`/api/py/universe/load/${job.job_id}/stop`, { method: "POST" });
      setJob(j);
      note(`Stopped: ${j.counts.cancelled} symbol(s) cancelled`, "warn");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not stop");
    }
  }

  async function resumeLoad() {
    if (!job) return;
    try {
      const j = await api<Job>(`/api/py/universe/load/${job.job_id}/resume`, { method: "POST" });
      setJob(j);
      await runSteps(j.job_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not resume");
    }
  }

  const itemBySymbol = useMemo(() => new Map((job?.items ?? []).map((i) => [i.symbol, i])), [job]);
  const tiles = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return (universe?.items ?? []).filter((c) => {
      if (q && !`${c.symbol} ${c.name ?? ""} ${c.sector ?? ""}`.toLowerCase().includes(q)) return false;
      if (statusFilter === "all") return true;
      const it = itemBySymbol.get(c.symbol);
      const st = it ? it.status : c.state;
      return st === statusFilter;
    });
  }, [universe, filter, statusFilter, itemBySymbol]);

  const c = job?.counts;
  const progress = c && c.total ? ((c.done + c.failed + c.skipped + c.cancelled) / c.total) * 100 : 0;
  const avgBatch = batchTimes.current.length ? batchTimes.current.reduce((a, b) => a + b, 0) / batchTimes.current.length : null;
  const eta = c && avgBatch && job ? Math.round((c.pending / (job.options.batch_size ?? 5)) * avgBatch / 1000) : null;
  const sel = selected ? universe?.items.find((i) => i.symbol === selected) : null;
  const selItem = selected ? itemBySymbol.get(selected) : null;

  return (
    <div className="single" style={{ maxWidth: 1200 }}>
      <DiagnosticsPanel diag={diag} error={diagError} showAi={false} onLog={(entries) => setLog((prev) => [...prev, ...entries])} />

      <div className="filters" style={{ marginBottom: 8 }}>
        <div className="stat">
          <span className="stat-label">S&amp;P 500 list</span>
          <span className="stat-value small">{universe ? `${universe.summary.total} constituents` : "—"}</span>
          <span className="tile-note">{universe?.list_source ? `dbo.SP500_CONSTITUENTS · ${universe.list_source} · updated ${when(universe.list_updated_at)}` : "dbo.SP500_CONSTITUENTS"}</span>
        </div>
        <div className="stat"><span className="stat-label">Ready</span><span className="stat-value" style={{ color: "#0ca30c" }}>{universe?.summary.ready ?? "—"}</span><span className="tile-note">prices and technical data current</span></div>
        <div className="stat"><span className="stat-label">Stale</span><span className="stat-value" style={{ color: "#c98500" }}>{universe?.summary.stale ?? "—"}</span><span className="tile-note">prices stored, update needed</span></div>
        <div className="stat"><span className="stat-label">Missing</span><span className="stat-value" style={{ color: "var(--muted)" }}>{universe?.summary.missing ?? "—"}</span><span className="tile-note">nothing stored yet</span></div>
      </div>

      <div className="filters" style={{ marginBottom: 6 }}>
        <label>
          Batch size
          <select value={batchSize} onChange={(e) => setBatchSize(Number(e.target.value))} disabled={running} style={{ minWidth: 80 }}>
            {[1, 2, 3, 5, 8, 10, 15, 20].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="toggle" style={{ alignSelf: "flex-end", paddingBottom: 8 }}>
          <input type="checkbox" checked={onlyStale} onChange={(e) => setOnlyStale(e.target.checked)} disabled={running} /> only missing/stale (delta)
        </label>
        <button type="button" className="btn" disabled={busy !== null || running || !universe?.summary.total} onClick={startLoad}>
          {busy === "start" ? "Starting…" : "Load / update all"}
        </button>
        <button type="button" className="btn small danger" disabled={!canStop} onClick={stopLoad} title={canStop ? "Stop the load now; remaining symbols are cancelled and can be resumed later" : "Nothing is running"}>
          Stop
        </button>
        {job && !running && (job.counts.pending > 0 || job.counts.cancelled > 0 || job.counts.failed > 0) && (
          <button type="button" className="btn small" onClick={resumeLoad}>
            {job.counts.pending > 0 ? "Resume" : "Retry failed / cancelled"}
          </button>
        )}
      </div>
      <p className="msg hint">
        Per symbol: prices are fetched only for days after the last stored close (two years when nothing is stored), and the
        technical data (TECH_ tables) is recomputed only when it is older than the latest stored close. Each batch is one
        request; you can stop and resume. The constituents come from dbo.SP500_CONSTITUENTS, which is treated as up to date (seeded from the repository list on first use).
      </p>

      {error && <p className="msg error">{error}</p>}

      {job && c && (
        <div className="progress-row">
          <div className="progress"><div className="progress-bar" style={{ width: `${progress}%` }} /></div>
          <span className="progress-text">
            Job #{job.job_id} · {c.done} done · {c.skipped} skipped · {c.failed} failed · {c.pending + c.running} remaining of {c.total}
            {eta !== null && c.pending > 0 && <> · ~{eta >= 60 ? `${Math.round(eta / 60)} min` : `${eta} s`} left</>}
            {job.status === "stopped" && <span className="stopped-chip">stopped</span>}
            {job.status === "done" && <span className="stopped-chip" style={{ background: "#0ca30c" }}>complete</span>}
            {(running || active) && job.status === "active" && <span className="spinner inline" aria-hidden />}
          </span>
        </div>
      )}

      <LiveLog title={job ? `Load log - job #${job.job_id}` : "Log"} entries={log} running={running || active} waitHint="waiting for the next batch (each batch is one request; a symbol takes 2-8 s)" />

      <div className="filters" style={{ margin: "12px 0 8px" }}>
        <label>
          Find
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="symbol, name, sector" style={{ minWidth: 220 }} />
        </label>
        <div className="seg" role="group" aria-label="Status filter">
          {["all", "ready", "stale", "missing", "pending", "running", "done", "skipped", "failed"].map((s) => (
            <button key={s} type="button" className={statusFilter === s ? "on" : ""} onClick={() => setStatusFilter(s)}>{s}</button>
          ))}
        </div>
        <span className="msg hint" style={{ margin: 0 }}>{tiles.length} shown</span>
      </div>
      <div className="cal-legend">
        <span><i className="cal-key" /> missing</span><span><i className="cal-key u-stale" /> stale</span><span><i className="cal-key u-ready" /> ready</span>
        <span><i className="cal-key running" /> loading</span><span><i className="cal-key done" /> loaded this job</span><span><i className="cal-key u-skipped" /> skipped (current)</span><span><i className="cal-key failed" /> failed</span>
      </div>

      {sel && (
        <div className="card u-detail">
          <strong>{sel.symbol}</strong> {sel.name ?? ""} <span className="tile-note">{sel.sector ?? ""}{sel.sub_industry ? ` · ${sel.sub_industry}` : ""}</span>
          <div className="tile-note" style={{ marginTop: 4 }}>
            Prices: {sel.price_rows ? `${sel.price_rows} rows, ${sel.price_first} → ${sel.price_last}` : "none stored"} · Technical data:{" "}
            {sel.tech_last ? `to ${sel.tech_last} (computed ${when(sel.tech_computed_at)})${sel.tech_fresh ? "" : " - stale"}` : "none"}
            {selItem && <> · This job: {selItem.status}{selItem.message ? ` - ${selItem.message}` : ""}{selItem.duration_ms ? ` (${(selItem.duration_ms / 1000).toFixed(1)} s)` : ""}</>}
          </div>
          <button type="button" className="linklike" style={{ marginTop: 4 }} onClick={() => setSelected(null)}>close</button>
        </div>
      )}

      <div className="u-grid">
        {tiles.map((t) => {
          const it = itemBySymbol.get(t.symbol);
          const cls = it ? `j-${it.status}` : `s-${t.state}`;
          const title = `${t.symbol} ${t.name ?? ""}\n${t.sector ?? ""}\nprices: ${t.price_rows ? `${t.price_rows} rows to ${t.price_last}` : "none"}\ntechnical: ${t.tech_last ? `to ${t.tech_last}` : "none"}${it ? `\njob: ${it.status}${it.message ? " - " + it.message : ""}` : ""}`;
          return (
            <button key={t.symbol} type="button" className={`u-tile ${cls} ${selected === t.symbol ? "sel" : ""}`} title={title} onClick={() => setSelected(t.symbol)}>
              {t.symbol}
            </button>
          );
        })}
      </div>
      {universe && universe.summary.total === 0 && (
        <p className="msg">dbo.SP500_CONSTITUENTS is empty and the seed file was not found; run scripts/seed_sp500_constituents.sql.</p>
      )}
    </div>
  );
}
