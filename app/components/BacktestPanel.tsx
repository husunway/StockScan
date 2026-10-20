"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

type Skill = { id: string; name: string; description: string; version: string };
type Counts = { pending: number; running: number; done: number; failed: number; total: number };
type Decision = {
  decision_id: number;
  as_of_date: string;
  status: "pending" | "running" | "done" | "failed";
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
  counts: Counts;
  decisions: Decision[];
};
type Previous = { backtest_id: number; skill_id: string; symbol: string; year: number; created_at: string; done: number; failed: number; total: number };

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
  const stopRef = useRef(false);

  useEffect(() => {
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
    const t = setTimeout(() => loadPrevious(symbol.trim(), skillId), 400);
    return () => clearTimeout(t);
  }, [symbol, skillId, loadPrevious]);

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
        const r = await api<{ done: boolean; decision: Decision | null; counts: Counts }>(
          `/api/py/agent/backtest/${id}/step${retryFailed ? "?retry_failed=true" : ""}`,
          { method: "POST" }
        );
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
      setError(e instanceof Error ? e.message : "Step failed");
    } finally {
      setRunning(false);
    }
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    const s = symbol.trim();
    if (!s || !skillId) return;
    setCreating(true);
    setError(null);
    setBt(null);
    try {
      const created = await api<Backtest>("/api/py/agent/backtest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill: skillId, symbol: s, year, weeks }),
      });
      setBt(created);
      setCreating(false);
      loadPrevious(s, skillId);
      await runSteps(created.backtest_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create backtest");
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

  return (
    <div className="single">
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

      {bt && c && (
        <div className="buffett">
          <div className="progress-row">
            <div className="progress"><div className="progress-bar" style={{ width: `${progress}%` }} /></div>
            <span className="progress-text">
              #{bt.backtest_id} {bt.symbol} · {c.done} done · {c.failed} failed · {c.pending + c.running} remaining of {c.total}
              {(running || active) && <span className="spinner inline" aria-hidden />}
            </span>
            {running ? (
              <button type="button" className="btn small ghost" onClick={() => { stopRef.current = true; }}>Stop after current</button>
            ) : (
              <>
                {c.pending > 0 && <button type="button" className="btn small" onClick={() => runSteps(bt.backtest_id)}>Resume</button>}
                {c.failed > 0 && <button type="button" className="btn small ghost" onClick={() => runSteps(bt.backtest_id, true)}>Retry failed</button>}
              </>
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
