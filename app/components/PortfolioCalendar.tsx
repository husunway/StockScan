"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { api } from "./RunUi";

type Snapshot = { trade_date: string; cash: number; market_value: number; total_value: number; realized_pnl: number; positions: number };
type TradeDay = { trade_date: string; symbol: string; side: "BUY" | "SELL"; count: number; realized_pnl: number };
export type CalendarJob = {
  job_id: number; status: string; start_date: string; end_date: string; days: string[]; backtests: Record<string, number>;
  config: { starting_capital: number }; snapshots: Snapshot[]; trade_days: TradeDay[];
  ai_costs?: { trade_date: string; cost: number; calls: number }[]; ai_cost_currency?: string;
};
type AgentInfo = {
  buy_price: number | null; target_price: number | null; stop_price: number | null; recommendation: string | null;
  run_date: string | null; mode: string | null; fresh: boolean; decision_id: number | null;
} | null;
type DayTrade = {
  trade_id: number; symbol: string; side: "BUY" | "SELL"; quantity: number; price: number; amount: number; reason: string | null;
  realized_pnl: number | null; realized_pct: number | null; cash_after: number; trigger: "consensus" | "target" | "take_profit";
  agents: Record<string, AgentInfo>;
};
type DayRun = {
  assessment_id: number; symbol: string; skill_id: string; phase: string; mode: string; decision_id: number | null; recommendation: string | null;
  current_price: number | null; buy_price: number | null; stop_price: number | null; target_price: number | null; is_buy: boolean;
  note: string | null; run_date: string;
  close: number | null; model: string | null; input_tokens: number | null; output_tokens: number | null;
  reused_from_id: number | null; model_call: boolean; est_cost: number | null;
};
type AiCost = {
  total: number; calls: number; reused: number; currency: string; prices_as_of: string | null; unpriced: boolean;
  by_agent: Record<string, { cost: number; calls: number; reused: number }>;
};
type DayDetail = {
  date: string; trading: boolean; simulated: boolean; backtests?: Record<string, number>;
  summary?: { start_value: number; end_value: number; change: number; change_pct: number | null; cash: number; market_value: number;
    positions: number; buys: number; sells: number; realized_pnl: number; realized_to_date: number };
  trades?: DayTrade[]; assessments?: DayRun[]; ai_cost?: AiCost;
};

const AGENTS = [["warren-buffett", "Buffett"], ["jamie-dimon", "Dimon"], ["john-murphy", "Murphy"]] as const;
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const TRIGGER = { consensus: "3-agent consensus", target: "Target price", take_profit: "Take profit" } as const;
const MODE_LABEL: Record<string, string> = { run: "fresh run", rerun: "fresh rerun", reassess: "held-stock reassessment", cached: "cached", skipped: "skipped", failed: "failed" };
// Heat-map steps on the absolute daily % change of total portfolio value.
const HEAT_STEPS = [0.05, 0.25, 0.5, 1];

const pad = (n: number) => String(n).padStart(2, "0");
const iso = (y: number, m: number, d: number) => `${y}-${pad(m + 1)}-${pad(d)}`;
const money = (v: number | null | undefined, nd = 2) => (v === null || v === undefined ? "—" : v.toLocaleString("en-CA", { minimumFractionDigits: nd, maximumFractionDigits: nd }));
const signed = (v: number | null | undefined, nd = 2) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${money(Math.abs(v), nd)}`);
const pct = (v: number | null | undefined, nd = 2) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(nd)}%`);
const tone = (v: number | null | undefined) => (!v ? "" : v > 0 ? "pos" : "neg");
const usd = (v: number | null | undefined, cur = "USD") => (v === null || v === undefined ? "—" : `${cur === "USD" ? "US$" : `${cur} `}${v.toFixed(v < 1 ? 4 : 2)}`);
function compact(v: number): string {
  const a = Math.abs(v);
  const s = a >= 1e6 ? `${(a / 1e6).toFixed(2)}M` : a >= 1e3 ? `${(a / 1e3).toFixed(1)}K` : a.toFixed(0);
  return `${v >= 0 ? "+" : "−"}$${s}`;
}
function heat(p: number | null): string {
  if (p === null || Math.abs(p) < HEAT_STEPS[0]) return "";
  const lvl = HEAT_STEPS.filter((s) => Math.abs(p) >= s).length;
  return `heat-${p > 0 ? "up" : "down"}-${lvl}`;
}
function monthCells(y: number, m: number): (number | null)[] {
  const offset = (new Date(y, m, 1).getDay() + 6) % 7;
  const n = new Date(y, m + 1, 0).getDate();
  return [...Array<null>(offset).fill(null), ...Array.from({ length: n }, (_, i) => i + 1)];
}
function monthsBetween(start: string, end: string): { y: number; m: number }[] {
  const out = [];
  let y = Number(start.slice(0, 4)), m = Number(start.slice(5, 7)) - 1;
  const ey = Number(end.slice(0, 4)), em = Number(end.slice(5, 7)) - 1;
  while (y < ey || (y === ey && m <= em)) {
    out.push({ y, m });
    m += 1;
    if (m > 11) { m = 0; y += 1; }
  }
  return out;
}
function csvCell(v: unknown): string {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function AgentCell({ a, day, href }: { a: AgentInfo | undefined; day: string; href: string | null }) {
  if (!a) return <span className="dim">—</span>;
  const label = a.fresh ? "fresh" : a.mode === "cached" || (a.run_date && a.run_date !== day) ? "cached" : a.mode ?? "";
  const title = `${a.recommendation ?? "?"} · buy at/below ${money(a.buy_price)} · target ${money(a.target_price)} · stop ${money(a.stop_price)}\n` +
    `assessment run ${a.run_date ?? "?"} (${MODE_LABEL[a.mode ?? ""] ?? a.mode ?? "?"})`;
  const body = (
    <span title={title}>
      {money(a.buy_price)} / {money(a.target_price)}
      <span className={`agent-tag ${a.fresh ? "fresh" : "cached"}`}>{label} {a.run_date ? a.run_date.slice(5) : ""}</span>
    </span>
  );
  return href ? <Link href={href}>{body}</Link> : body;
}

export default function PortfolioCalendar({ job }: { job: CalendarJob }) {
  const months = useMemo(() => monthsBetween(job.start_date, job.end_date), [job.start_date, job.end_date]);
  const tradingDays = useMemo(() => new Set(job.days), [job.days]);
  const [filter, setFilter] = useState<string | null>(null);
  const [monthIdx, setMonthIdx] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<DayDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const followLive = useRef(true);

  // Daily change from consecutive end-of-day snapshots (the first day is compared with the starting capital).
  const dayStats = useMemo(() => {
    const map = new Map<string, { total: number; change: number; pct: number | null }>();
    let prev = job.config.starting_capital;
    for (const s of job.snapshots) {
      map.set(s.trade_date, { total: s.total_value, change: s.total_value - prev, pct: prev ? ((s.total_value - prev) / prev) * 100 : null });
      prev = s.total_value;
    }
    return map;
  }, [job.snapshots, job.config.starting_capital]);
  const counts = useMemo(() => {
    const map = new Map<string, { buys: number; sells: number }>();
    for (const t of job.trade_days) {
      if (filter && t.symbol !== filter) continue;
      const c = map.get(t.trade_date) ?? { buys: 0, sells: 0 };
      if (t.side === "BUY") c.buys += t.count; else c.sells += t.count;
      map.set(t.trade_date, c);
    }
    return map;
  }, [job.trade_days, filter]);
  const tickers = useMemo(() => [...new Set(job.trade_days.map((t) => t.symbol))].sort(), [job.trade_days]);
  const costByDay = useMemo(() => new Map((job.ai_costs ?? []).map((c) => [c.trade_date, c])), [job.ai_costs]);
  const currency = job.ai_cost_currency ?? "USD";

  // Open on the latest simulated month and follow the simulation until the user navigates.
  const lastSnap = job.snapshots.length ? job.snapshots[job.snapshots.length - 1].trade_date : job.start_date;
  useEffect(() => {
    if (!followLive.current) return;
    const i = months.findIndex(({ y, m }) => iso(y, m, 1).slice(0, 7) === lastSnap.slice(0, 7));
    if (i >= 0) setMonthIdx(i);
  }, [lastSnap, months]);
  useEffect(() => { followLive.current = true; setSelected(null); setDetail(null); setFilter(null); }, [job.job_id]);

  const selectedSimulated = selected ? dayStats.has(selected) : false;
  useEffect(() => {
    if (!selected || !tradingDays.has(selected)) return;
    let cancelled = false;
    setLoading(true);
    setDetailError(null);
    api<DayDetail>(`/api/py/portfolio/${job.job_id}/day/${selected}`)
      .then((d) => { if (!cancelled) setDetail(d); })
      .catch((e: Error) => { if (!cancelled) setDetailError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // Refetch when the selected day becomes simulated during a live run.
  }, [selected, selectedSimulated, job.job_id, tradingDays]);

  function go(i: number) {
    followLive.current = false;
    setMonthIdx(Math.max(0, Math.min(months.length - 1, i)));
  }
  function exportCsv() {
    if (!detail?.trades) return;
    const head = ["date", "ticker", "action", "quantity", "execution_price", "amount", "trigger",
      ...AGENTS.flatMap(([, n]) => [`${n.toLowerCase()}_buy_price`, `${n.toLowerCase()}_target_price`, `${n.toLowerCase()}_assessment_date`, `${n.toLowerCase()}_status`]),
      "realized_pnl", "realized_pnl_pct", "cash_after", "reason"];
    const rows = rowsShown.map((t) => [detail.date, t.symbol, t.side, t.quantity, t.price, t.amount, TRIGGER[t.trigger],
      ...AGENTS.flatMap(([k]) => { const a = t.agents?.[k]; return [a?.buy_price, a?.target_price, a?.run_date, a ? (a.fresh ? "fresh" : "cached") : ""]; }),
      t.realized_pnl, t.realized_pct, t.cash_after, t.reason]);
    const csv = [head, ...rows].map((r) => r.map(csvCell).join(",")).join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `portfolio-${job.job_id}-${detail.date}${filter ? `-${filter}` : ""}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  if (!months.length) return null;
  const { y, m } = months[monthIdx] ?? months[0];
  const inRange = (d: string) => d >= job.start_date && d <= job.end_date;
  const decisionHref = (skill: string, id: number | null | undefined) => (id ? `/agent/backtest/decision?bt=${job.backtests[skill]}&id=${id}` : null);
  const rowsShown = (detail?.trades ?? []).filter((t) => !filter || t.symbol === filter);
  const runsShown = (detail?.assessments ?? []).filter((r) => !filter || r.symbol === filter);

  return (
    <div className="pcal-layout">
      <div className="card pcal">
        <div className="pcal-head">
          <button type="button" className="btn small ghost" onClick={() => go(monthIdx - 1)} disabled={monthIdx === 0} aria-label="Previous month">‹</button>
          <select value={monthIdx} onChange={(e) => go(Number(e.target.value))} aria-label="Jump to month">
            {months.map((mo, i) => <option key={i} value={i}>{MONTHS[mo.m]} {mo.y}</option>)}
          </select>
          <button type="button" className="btn small ghost" onClick={() => go(monthIdx + 1)} disabled={monthIdx === months.length - 1} aria-label="Next month">›</button>
          {job.status === "active" && <button type="button" className="linklike" onClick={() => { followLive.current = true; const i = months.findIndex((mo) => iso(mo.y, mo.m, 1).slice(0, 7) === lastSnap.slice(0, 7)); if (i >= 0) setMonthIdx(i); }}>follow live</button>}
          <span className="pcal-filter">
            Stock
            <select value={filter ?? ""} onChange={(e) => setFilter(e.target.value || null)}>
              <option value="">all</option>
              {tickers.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
            {filter && <button type="button" className="linklike" onClick={() => setFilter(null)}>clear</button>}
          </span>
        </div>
        <div className="pcal-grid">
          {DOW.map((d) => <span key={d} className="pcal-dow">{d}</span>)}
          {monthCells(y, m).map((day, i) => {
            if (day === null) return <span key={`e${i}`} className="pcal-cell empty" />;
            const date = iso(y, m, day);
            const trading = tradingDays.has(date);
            const st = dayStats.get(date);
            const c = counts.get(date);
            const ai = costByDay.get(date);
            const cls = !inRange(date) ? "out" : !trading ? "closed" : !st ? "pending" : heat(st.pct);
            return (
              <button type="button" key={date} className={`pcal-cell ${cls} ${selected === date ? "sel" : ""}`} onClick={() => setSelected(date)}
                title={!inRange(date) ? `${date}: outside the backtest range` : !trading ? `${date}: market closed` : !st ? `${date}: not simulated yet`
                  : `${date}: ${signed(st.change)} (${pct(st.pct)}), total ${money(st.total, 0)}${c ? ` · ${c.buys} buy(s), ${c.sells} sell(s)` : ""}`
                    + ` · est. AI cost ${usd(ai?.cost ?? 0, currency)} (${ai?.calls ?? 0} model call${ai?.calls === 1 ? "" : "s"})`}>
                <span className="pcal-day">{day}</span>
                {c && (c.buys > 0 || c.sells > 0) && (
                  <span className="pcal-marks">
                    {c.buys > 0 && <span className="mark-buy">▲{c.buys}</span>}
                    {c.sells > 0 && <span className="mark-sell">▼{c.sells}</span>}
                  </span>
                )}
                {st && (
                  <span className={`pcal-change ${tone(st.change)}`}>
                    <span>{compact(st.change)}</span><span>{pct(st.pct)}</span>
                  </span>
                )}
                {trading && !st && inRange(date) && <span className="pcal-pending">…</span>}
              </button>
            );
          })}
        </div>
        <div className="pcal-legend">
          <span><span className="mark-buy">▲</span> buys</span><span><span className="mark-sell">▼</span> sells</span>
          <span className="pcal-scale">
            daily change of total value:
            {[...HEAT_STEPS].reverse().map((s, i) => <i key={`d${s}`} className={`pcal-key heat-down-${HEAT_STEPS.length - i}`} title={`loss ≥ ${s}%`} />)}
            <i className="pcal-key" title={`under ${HEAT_STEPS[0]}%`} />
            {HEAT_STEPS.map((s, i) => <i key={`u${s}`} className={`pcal-key heat-up-${i + 1}`} title={`gain ≥ ${s}%`} />)}
            <span className="tile-note">−{HEAT_STEPS[HEAT_STEPS.length - 1]}% … +{HEAT_STEPS[HEAT_STEPS.length - 1]}% and beyond</span>
          </span>
          <span><i className="pcal-key closed" /> market closed</span><span><i className="pcal-key pending" /> not simulated yet</span>
        </div>
      </div>

      <div className="card pcal-detail">
        {!selected ? <p className="msg">Select a day to see its summary, transactions and agent assessments.</p>
          : !inRange(selected) ? <p className="msg"><strong>{selected}</strong> is outside the backtest range.</p>
          : !tradingDays.has(selected) ? <p className="msg"><strong>{selected}</strong> — Market closed.</p>
          : loading && !detail ? <p className="msg">Loading {selected}…</p>
          : detailError ? <p className="msg error">{detailError}</p>
          : !detail || detail.date !== selected ? <p className="msg">Loading {selected}…</p>
          : !detail.simulated || !detail.summary ? <p className="msg"><strong>{selected}</strong> — not simulated yet.</p>
          : (
            <>
              <h3 className="pcal-title">{new Date(`${selected}T12:00:00`).toLocaleDateString("en-CA", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}</h3>
              <div className="pcal-summary">
                <div><span className="stat-label">Start of day</span><strong>{money(detail.summary.start_value, 0)}</strong></div>
                <div><span className="stat-label">End of day</span><strong>{money(detail.summary.end_value, 0)}</strong></div>
                <div><span className="stat-label">Change</span><strong className={tone(detail.summary.change)}>{signed(detail.summary.change)} ({pct(detail.summary.change_pct)})</strong></div>
                <div><span className="stat-label">Cash (end of day)</span><strong>{money(detail.summary.cash, 0)}</strong></div>
                <div><span className="stat-label">Buys / sells</span><strong>{detail.summary.buys} / {detail.summary.sells}</strong></div>
                <div><span className="stat-label">Realized P&amp;L (day)</span><strong className={tone(detail.summary.realized_pnl)}>{signed(detail.summary.realized_pnl)}</strong></div>
              </div>
              {detail.ai_cost && (
                <div className="pcal-cost" title={`Estimate: tokens × configured list prices${detail.ai_cost.prices_as_of ? ` (as of ${detail.ai_cost.prices_as_of})` : ""}; not the provider's invoice`}>
                  <span className="stat-label">Estimated AI cost (day)</span>
                  <strong>{usd(detail.ai_cost.total, detail.ai_cost.currency)}</strong>
                  <span className="tile-note">
                    {detail.ai_cost.calls} model call{detail.ai_cost.calls === 1 ? "" : "s"}{detail.ai_cost.reused ? ` · ${detail.ai_cost.reused} reused (no cost)` : ""}
                    {AGENTS.filter(([k]) => detail.ai_cost!.by_agent[k]).map(([k, n]) => ` · ${n} ${usd(detail.ai_cost!.by_agent[k].cost, detail.ai_cost!.currency)}`).join("")}
                    {detail.ai_cost.unpriced && " · some calls use a model with no configured price"}
                  </span>
                </div>
              )}

              <div className="pcal-subhead">
                <h4>Transactions {filter ? `— ${filter}` : ""} ({rowsShown.length})</h4>
                {rowsShown.length > 0 && <button type="button" className="btn small ghost" onClick={exportCsv}>Export CSV</button>}
              </div>
              {rowsShown.length === 0 ? <p className="msg">No trades on this day.</p> : (
                <div className="table-wrap">
                  <table className="table compact">
                    <thead><tr><th>Ticker</th><th>Action</th><th className="num">Qty</th><th className="num">Exec price</th><th className="num">Amount</th><th>Trigger</th>
                      {AGENTS.map(([, n]) => <th key={n}>{n} buy / target</th>)}<th className="num">Realized P&amp;L</th></tr></thead>
                    <tbody>
                      {rowsShown.map((t) => (
                        <tr key={t.trade_id} title={t.reason ?? ""}>
                          <td><button type="button" className="linklike" title="Show only this stock on the calendar" onClick={() => setFilter(t.symbol)}><strong>{t.symbol}</strong></button></td>
                          <td><span className={`rec-pill ${t.side === "BUY" ? "rec-buy" : "rec-sell"}`}>{t.side === "BUY" ? "Buy" : "Sell"}</span></td>
                          <td className="num">{t.quantity.toLocaleString()}</td><td className="num">{money(t.price)}</td><td className="num">{money(t.amount)}</td>
                          <td>{TRIGGER[t.trigger] ?? t.trigger}</td>
                          {AGENTS.map(([k]) => <td key={k}><AgentCell a={t.agents?.[k]} day={selected} href={decisionHref(k, t.agents?.[k]?.decision_id)} /></td>)}
                          <td className={`num ${tone(t.realized_pnl)}`}>{t.side === "SELL" ? <>{signed(t.realized_pnl)}<br /><span className="tile-note">{pct(t.realized_pct)}</span></> : ""}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <details className="pcal-activity">
                <summary>Agent activity on this day ({runsShown.length})</summary>
                {runsShown.length === 0 ? <p className="msg">No agent runs.</p> : (
                  <div className="table-wrap">
                    <table className="table compact">
                      <thead><tr><th>Ticker</th><th>Agent</th><th className="num" title="Previous close: the price the agent decided on">Decision price</th><th className="num" title="This day's closing price">Close</th><th className="num">Buy price</th><th className="num">Target</th><th>Decision</th><th>Status</th><th className="num">Est. cost</th></tr></thead>
                      <tbody>
                        {runsShown.map((r) => {
                          const name = AGENTS.find(([k]) => k === r.skill_id)?.[1] ?? r.skill_id;
                          const href = decisionHref(r.skill_id, r.decision_id);
                          return (
                            <tr key={r.assessment_id} className={r.mode === "failed" ? "row-failed" : ""} title={r.note ?? ""}>
                              <td><strong>{r.symbol}</strong></td>
                              <td>{href ? <Link href={href}>{name}</Link> : name}</td>
                              <td className="num">{money(r.current_price)}</td><td className="num">{money(r.close)}</td>
                              <td className="num">{money(r.buy_price)}</td><td className="num">{money(r.target_price)}</td>
                              <td>{r.is_buy ? <strong className="pos">BUY</strong> : <span className="dim">NOT BUY</span>}{r.recommendation ? <span className="tile-note"> ({r.recommendation})</span> : null}</td>
                              <td>{MODE_LABEL[r.mode] ?? r.mode}{r.reused_from_id ? <span className="agent-tag">reused from #{r.reused_from_id}</span> : null}{r.mode === "cached" || r.mode === "skipped" ? <span className="tile-note"> from {r.run_date}</span> : null}{r.phase === "sell" ? <span className="tile-note"> · sell check</span> : null}</td>
                              <td className="num" title={r.model_call ? `${r.model ?? "?"}: ${r.input_tokens ?? 0} input + ${r.output_tokens ?? 0} output tokens` : "no model call"}>{r.model_call ? usd(r.est_cost, currency) : usd(0, currency)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </details>
            </>
          )}
      </div>
    </div>
  );
}
