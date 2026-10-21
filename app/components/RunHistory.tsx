"use client";

import { Fragment, useCallback, useEffect, useState } from "react";
import Link from "next/link";

type Failure = { as_of_date: string; status: string; error: string; decision_id: number };
type Run = {
  backtest_id: number;
  skill_id: string;
  skill_version: string | null;
  symbol: string;
  year: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  first_monday: string | null;
  last_monday: string | null;
  status: "success" | "failed" | "in progress" | "stopped" | "partial";
  backtest_status: string;
  counts: { total: number; done: number; failed: number; cancelled: number; pending: number; running: number };
  verdicts: { BUY: number; HOLD: number; SELL: number };
  tokens: { input: number; output: number };
  failures: Failure[];
};
type Decision = {
  decision_id: number;
  as_of_date: string;
  status: string;
  recommendation: string | null;
  conviction: string | null;
  current_price: number | null;
  target_price: number | null;
  stop_price: number | null;
  headline: string | null;
  error: string | null;
  model: string | null;
  started_at: string | null;
  finished_at: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
};

const when = (iso: string | null) => (iso ? new Date(iso.endsWith("Z") ? iso : iso + "Z").toLocaleString() : "—");
const dur = (s: number | null) => (s === null ? "—" : s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`);
const n = (v: number | null) => (v === null ? "—" : v.toFixed(2));
const TONE: Record<Run["status"], string> = { success: "good", failed: "bad", "in progress": "info", stopped: "warn", partial: "warn" };

export default function RunHistory() {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [detail, setDetail] = useState<Record<number, Decision[]>>({});

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/py/agent/runs");
      const d = await r.json();
      if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not load runs");
      setRuns(d.runs);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load runs");
      setRuns([]);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Keep in-progress rows fresh.
  useEffect(() => {
    if (!runs?.some((r) => r.status === "in progress")) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [runs, load]);

  async function toggle(id: number) {
    if (open === id) return setOpen(null);
    setOpen(id);
    if (!detail[id]) {
      try {
        const r = await fetch(`/api/py/agent/backtest/${id}`);
        const d = await r.json();
        if (r.ok) setDetail((prev) => ({ ...prev, [id]: d.decisions }));
      } catch {}
    }
  }

  if (error) return <p className="msg error">{error}</p>;
  if (!runs) return <p className="msg">Loading runs…</p>;

  const q = filter.trim().toLowerCase();
  const shown = q ? runs.filter((r) => `${r.symbol} ${r.skill_id} ${r.status} ${r.year}`.toLowerCase().includes(q)) : runs;

  return (
    <div className="single">
      <div className="filters" style={{ marginBottom: 10 }}>
        <label>
          Filter
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="symbol, agent, status, year" style={{ minWidth: 240 }} />
        </label>
        <button type="button" className="btn small ghost" onClick={load}>Refresh</button>
        <span className="msg hint" style={{ margin: 0 }}>{shown.length} of {runs.length} runs</span>
      </div>
      {runs.length === 0 && <p className="msg">No runs yet.</p>}
      {runs.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>Agent</th>
                <th>Symbol</th>
                <th>Created</th>
                <th>Date range (Mondays)</th>
                <th className="num">Mondays</th>
                <th>Verdicts</th>
                <th>Duration</th>
                <th>Status</th>
                <th>Reason of failure</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <Fragment key={r.backtest_id}>
                  <tr className={open === r.backtest_id ? "row-open" : ""} onClick={() => toggle(r.backtest_id)} style={{ cursor: "pointer" }}>
                    <td>{r.backtest_id}</td>
                    <td>{r.skill_id}{r.skill_version ? <span className="tile-note"> v{r.skill_version}</span> : null}</td>
                    <td><strong>{r.symbol}</strong></td>
                    <td>{when(r.created_at)}</td>
                    <td>{r.first_monday ?? "—"} → {r.last_monday ?? "—"}<span className="tile-note"> ({r.year})</span></td>
                    <td className="num">
                      {r.counts.done}/{r.counts.total}
                      {r.counts.failed > 0 && <span className="verdict-fail"> · {r.counts.failed} failed</span>}
                      {r.counts.cancelled > 0 && <span className="tile-note"> · {r.counts.cancelled} cancelled</span>}
                      {r.counts.pending + r.counts.running > 0 && <span className="tile-note"> · {r.counts.pending + r.counts.running} left</span>}
                    </td>
                    <td>
                      <span className="rec-pill rec-buy">B {r.verdicts.BUY}</span>{" "}
                      <span className="rec-pill rec-hold">H {r.verdicts.HOLD}</span>{" "}
                      <span className="rec-pill rec-sell">S {r.verdicts.SELL}</span>
                    </td>
                    <td>{dur(r.duration_s)}</td>
                    <td><span className={`status-pill ${TONE[r.status]}`}>{r.status}</span></td>
                    <td className="wrap">
                      {r.failures.length === 0 ? "—" : (
                        <ul className="fail-list">
                          {r.failures.slice(0, 3).map((f) => (
                            <li key={f.decision_id}><code>{f.as_of_date}</code> {f.error}</li>
                          ))}
                          {r.failures.length > 3 && <li className="tile-note">+{r.failures.length - 3} more (expand)</li>}
                        </ul>
                      )}
                    </td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <Link href={`/agent/backtest?skill=${r.skill_id}&symbol=${r.symbol}&bt=${r.backtest_id}`}>open</Link>
                    </td>
                  </tr>
                  {open === r.backtest_id && (
                    <tr className="row-detail">
                      <td colSpan={11}>
                        <div className="run-detail">
                          <div className="tile-note" style={{ marginBottom: 6 }}>
                            Started {when(r.started_at)} · finished {when(r.finished_at)} · run state {r.backtest_status} ·{" "}
                            {r.tokens.input.toLocaleString()} input / {r.tokens.output.toLocaleString()} output tokens
                          </div>
                          {!detail[r.backtest_id] ? (
                            <p className="msg">Loading Mondays…</p>
                          ) : (
                            <table className="table">
                              <thead>
                                <tr>
                                  <th>Monday</th><th>Status</th><th>Verdict</th><th>Conviction</th>
                                  <th className="num">Price</th><th className="num">Target</th><th className="num">Stop</th>
                                  <th>Started</th><th>Finished</th><th>Model</th><th>Headline / error</th><th></th>
                                </tr>
                              </thead>
                              <tbody>
                                {detail[r.backtest_id].map((d) => (
                                  <tr key={d.decision_id} className={`row-${d.status}`}>
                                    <td>{d.as_of_date}</td>
                                    <td>{d.status}</td>
                                    <td>{d.recommendation ? <span className={`rec-pill rec-${d.recommendation.toLowerCase()}`}>{d.recommendation}</span> : "—"}</td>
                                    <td>{d.conviction ?? "—"}</td>
                                    <td className="num">{n(d.current_price)}</td>
                                    <td className="num">{n(d.target_price)}</td>
                                    <td className="num">{n(d.stop_price)}</td>
                                    <td>{when(d.started_at)}</td>
                                    <td>{when(d.finished_at)}</td>
                                    <td>{d.model ?? "—"}</td>
                                    <td className="wrap">{d.error ? <span className="verdict-fail">{d.error}</span> : d.headline ?? ""}</td>
                                    <td>
                                      {(d.status === "done" || d.status === "failed") && (
                                        <Link href={`/agent/backtest/decision?bt=${r.backtest_id}&id=${d.decision_id}`}>detail</Link>
                                      )}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
