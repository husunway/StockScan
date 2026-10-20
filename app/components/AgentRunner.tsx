"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

type Skill = { id: string; name: string; description: string; version: string; path: string };
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
  // Skill-specific blocks: valuation keys differ per skill; balance_sheet / stress_test are optional.
  valuation: Record<string, string | number | null>;
  balance_sheet?: Record<string, string>;
  stress_test?: {
    scenario: string;
    assumptions: string;
    earnings_impact_pct: number;
    price_impact_pct: number;
    survives_without_capital_raise: boolean;
  }[];
  risks: string[];
  assumptions: string[];
  data_gaps: string[];
};

const label = (key: string) => key.replace(/_/g, " ").replace(/\bpct\b/g, "%").replace(/^\w/, (c) => c.toUpperCase());
const cell = (key: string, v: string | number | null) =>
  v === null || v === undefined
    ? "—"
    : typeof v === "number"
      ? `${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}${/pct|rate|yield/.test(key) ? "%" : ""}`
      : v;

type Result = {
  skill: { id: string; name: string; version: string; path: string };
  symbol: string;
  analysis: Analysis;
  warnings: string[];
  data_used: unknown;
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
const paragraphs = (text: string) => text.split(/\n{2,}/).filter(Boolean);

export default function AgentRunner() {
  const params = useSearchParams();
  const [skills, setSkills] = useState<Skill[] | null>(null);
  const [skillId, setSkillId] = useState(params.get("skill") ?? "");
  const [symbol, setSymbol] = useState(params.get("symbol")?.toUpperCase() ?? "");
  const [known, setKnown] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [showData, setShowData] = useState(false);

  useEffect(() => {
    fetch("/api/py/agent/skills")
      .then(async (r) => {
        const d = await r.json();
        if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not list skills");
        return d.skills as Skill[];
      })
      .then((list) => {
        setSkills(list);
        setSkillId((prev) => (prev && list.some((s) => s.id === prev) ? prev : list[0]?.id ?? ""));
      })
      .catch((e: Error) => {
        setSkills([]);
        setError(e.message);
      });
    fetch("/api/py/symbols")
      .then((r) => (r.ok ? r.json() : { symbols: [] }))
      .then((d) => setKnown((d.symbols ?? []).map((s: { symbol: string }) => s.symbol)))
      .catch(() => {});
  }, []);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    const s = symbol.trim();
    if (!s || !skillId) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch("/api/py/agent/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill: skillId, symbol: s }),
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

  const skill = skills?.find((s) => s.id === skillId);
  const a = result?.analysis;
  const ccy = a?.currency ?? "";
  const upside = a && a.target_price ? (a.target_price / a.current_price - 1) * 100 : null;
  const downside = a && a.stop_price ? (a.stop_price / a.current_price - 1) * 100 : null;

  return (
    <div className="single">
      <form className="filters agent-form" onSubmit={run}>
        <label>
          Skill
          <select value={skillId} onChange={(e) => setSkillId(e.target.value)} disabled={!skills?.length}>
            {skills?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Symbol
          <input
            list="known-symbols"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            placeholder="e.g. KO, AAPL, RY.TO"
            maxLength={20}
          />
          <datalist id="known-symbols">
            {known.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        </label>
        <button type="submit" className="btn" disabled={loading || !symbol.trim() || !skillId}>
          {loading ? "Running…" : "Run skill"}
        </button>
      </form>
      {skill && (
        <p className="msg hint">
          {skill.description}{" "}
          <Link href={`/agent/skill?id=${skill.id}`}>View skill file</Link> ({skill.path}, v{skill.version || "—"}).
          Usually takes 30–90 seconds.
        </p>
      )}

      {error && <p className="msg error">{error}</p>}
      {loading && (
        <div className="working">
          <span className="spinner" aria-hidden /> Gathering data and running {skill?.name ?? "the skill"}…
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
                  {result.skill.name} · Conviction <strong>{a.conviction}</strong> · Horizon {a.time_horizon} ·
                  Margin of safety <strong>{pct(a.margin_of_safety_pct)}</strong> (required{" "}
                  {a.required_margin_of_safety_pct.toFixed(0)}%)
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
              <div className={`stat ${a.recommendation !== "HOLD" ? "emph" : ""}`}>
                <span className="stat-label">Target price</span>
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

          <h2 className="section-title">Summary and justification</h2>
          {paragraphs(a.summary).map((p, i) => (
            <p key={i} className="prose">
              {p}
            </p>
          ))}

          {a.balance_sheet && (
            <>
              <h2 className="section-title">
                Balance sheet{" "}
                <span className={`grade grade-${a.balance_sheet.grade}`}>{a.balance_sheet.grade}</span>
              </h2>
              <div className="table-wrap">
                <table className="table">
                  <tbody>
                    {Object.entries(a.balance_sheet)
                      .filter(([k]) => k !== "grade")
                      .map(([k, v]) => (
                        <tr key={k}>
                          <td>{label(k)}</td>
                          <td className="wrap">{v}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {a.stress_test && a.stress_test.length > 0 && (
            <>
              <h2 className="section-title">Stress test</h2>
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Scenario</th>
                      <th>Assumptions</th>
                      <th className="num">Earnings impact</th>
                      <th className="num">Price impact</th>
                      <th>Survives without capital raise</th>
                    </tr>
                  </thead>
                  <tbody>
                    {a.stress_test.map((s) => (
                      <tr key={s.scenario}>
                        <td style={{ textTransform: "capitalize" }}>{s.scenario}</td>
                        <td className="wrap">{s.assumptions}</td>
                        <td className="num">{pct(s.earnings_impact_pct)}</td>
                        <td className="num">{pct(s.price_impact_pct)}</td>
                        <td className={s.survives_without_capital_raise ? "verdict-pass" : "verdict-fail"}>
                          {s.survives_without_capital_raise ? "✓ yes" : "✗ no"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <h2 className="section-title">Checklist</h2>
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
              {paragraphs(s.content).map((p, i) => (
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
                {Object.entries(a.valuation).map(([k, v]) => (
                  <tr key={k}>
                    <td>{label(k)}</td>
                    <td className="wrap">{cell(k, v)}</td>
                  </tr>
                ))}
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
            Generated {new Date(result.generated_at).toLocaleString()} by {result.model} using {result.skill.path} ·{" "}
            {result.usage.input_tokens.toLocaleString()} in / {result.usage.output_tokens.toLocaleString()} out tokens
            {" · "}
            <button type="button" className="linklike" onClick={() => setShowData((v) => !v)}>
              {showData ? "Hide" : "Show"} data sent to the model
            </button>
          </p>
          {showData && <pre className="data-dump">{JSON.stringify(result.data_used, null, 2)}</pre>}
          <p className="msg disclaimer">
            This is model-generated analysis based on third-party data that may be incomplete or out of date.
            It is not investment advice. Verify the figures and make your own decision.
          </p>
        </div>
      )}
    </div>
  );
}
