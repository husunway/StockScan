"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

type Decision = {
  decision_id: number;
  backtest_id: number;
  skill_id: string;
  symbol: string;
  as_of_date: string;
  status: string;
  recommendation: string | null;
  error: string | null;
  warnings: string[];
  model: string | null;
  input: unknown;
  output: Record<string, unknown> | null;
};

export default function DecisionView() {
  const params = useSearchParams();
  const bt = params.get("bt");
  const id = params.get("id");
  const [d, setD] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"output" | "input">("output");

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
  const text = (k: string) => (typeof out[k] === "string" ? (out[k] as string) : "");
  const list = (k: string) => (Array.isArray(out[k]) ? (out[k] as string[]) : []);

  return (
    <div className="single">
      <p className="msg hint">
        <Link href={`/agent/backtest?skill=${d.skill_id}&symbol=${d.symbol}`}>← Back to backtest #{d.backtest_id}</Link>
        {" · "}{d.symbol} as of {d.as_of_date} · {d.skill_id} · {d.model ?? ""}
      </p>
      {d.error && <p className="msg error">{d.error}</p>}
      {d.warnings.length > 0 && <ul className="warnings">{d.warnings.map((w) => <li key={w}>⚠ {w}</li>)}</ul>}
      {d.output && (
        <section className={`verdict rec-${String(out.recommendation ?? "").toLowerCase()}`}>
          <div className="verdict-head">
            <span className="rec-badge">{String(out.recommendation ?? "")}</span>
            <div>
              <h2>{text("headline")}</h2>
              <p className="verdict-meta">
                Conviction <strong>{text("conviction")}</strong> · Price {String(out.current_price ?? "—")} · Buy below {String(out.buy_below_price ?? "—")} ·
                Target {String(out.target_price ?? "—")} · Stop {String(out.stop_price ?? "—")} {text("currency")}
              </p>
            </div>
          </div>
          {text("summary").split(/\n{2,}/).map((p, i) => <p key={i} className="prose">{p}</p>)}
          {list("data_gaps").length > 0 && (
            <>
              <h2 className="section-title">Data gaps</h2>
              <ul className="list">{list("data_gaps").map((r) => <li key={r}>{r}</li>)}</ul>
            </>
          )}
        </section>
      )}
      <div className="seg" role="tablist" style={{ margin: "16px 0 10px" }}>
        <button type="button" className={tab === "output" ? "on" : ""} onClick={() => setTab("output")}>Full output</button>
        <button type="button" className={tab === "input" ? "on" : ""} onClick={() => setTab("input")}>Data the agent saw</button>
      </div>
      <pre className="data-dump" style={{ maxHeight: 640 }}>
        {JSON.stringify(tab === "output" ? d.output : d.input, null, 2)}
      </pre>
    </div>
  );
}
