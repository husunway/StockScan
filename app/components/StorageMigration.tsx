"use client";

import { useRef, useState } from "react";
import { api, LiveLog, type LogEntry } from "@/app/components/RunUi";

type Item = { pathname: string; size: number; present: boolean };
type Plan = {
  source: string; target: string; total: number; bytes: number; present: number; folders: number;
  target_files: number; list_seconds: number; items: Item[];
};
type CopyResult = { pathname: string; ok: boolean; bytes?: number; content_type?: string; seconds?: number; error?: string };

const BATCH = 20;

function fmtBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export default function StorageMigration() {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [running, setRunning] = useState(false);
  const [recopy, setRecopy] = useState(false);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [done, setDone] = useState({ copied: 0, failed: 0, bytes: 0, queued: 0 });
  const [failures, setFailures] = useState<CopyResult[]>([]);
  const stopRef = useRef(false);
  const t0 = useRef(0);

  const add = (message: string, level: LogEntry["level"] = "info") =>
    setLog((l) => [...l, { t: new Date().toISOString(), elapsed_s: (Date.now() - t0.current) / 1000, level, message }]);

  async function migrate() {
    stopRef.current = false;
    t0.current = Date.now();
    setRunning(true);
    setLog([]);
    setFailures([]);
    setDone({ copied: 0, failed: 0, bytes: 0, queued: 0 });
    try {
      add("Listing files in Vercel Blob and Azure Blob Storage ...");
      const p = await api<Plan>("/api/py/storage/migrate/plan", { cache: "no-store" });
      setPlan(p);
      add(`${p.source}: ${p.total} files (${fmtBytes(p.bytes)}) in ${p.folders} folders; ${p.target}: ${p.target_files} files; ` +
          `${p.present} already copied (same path and size). Listed in ${p.list_seconds} s.`);
      const todo = p.items.filter((i) => recopy || !i.present);
      setDone((d) => ({ ...d, queued: todo.length }));
      if (!todo.length) {
        add("Nothing to copy: every file is already in Azure with the same size.");
        return;
      }
      add(`Copying ${todo.length} file(s) in batches of ${BATCH}${recopy ? " (re-copying files already present)" : ""} ...`);
      let copied = 0, failed = 0, bytes = 0;
      for (let i = 0; i < todo.length; i += BATCH) {
        if (stopRef.current) {
          add(`Stopped by user after ${copied + failed} of ${todo.length} files. Press Migrate to continue; copied files are skipped.`, "warn");
          return;
        }
        const batch = todo.slice(i, i + BATCH);
        try {
          const r = await api<{ results: CopyResult[] }>("/api/py/storage/migrate/copy", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ pathnames: batch.map((b) => b.pathname) }),
          });
          const bad = r.results.filter((x) => !x.ok);
          copied += r.results.length - bad.length;
          failed += bad.length;
          bytes += r.results.reduce((s, x) => s + (x.bytes ?? 0), 0);
          if (bad.length) {
            setFailures((f) => [...f, ...bad]);
            for (const b of bad) add(`${b.pathname}: FAILED - ${b.error}`, "error");
          }
          add(`Batch ${i / BATCH + 1}: ${r.results.length - bad.length} copied, ${bad.length} failed ` +
              `(${Math.min(i + BATCH, todo.length)} of ${todo.length}); e.g. ${batch[0].pathname}`);
        } catch (e) {
          failed += batch.length;
          setFailures((f) => [...f, ...batch.map((b) => ({ pathname: b.pathname, ok: false, error: e instanceof Error ? e.message : String(e) }))]);
          add(`Batch ${i / BATCH + 1} failed: ${e instanceof Error ? e.message : e}; continuing with the next batch`, "error");
        }
        setDone({ copied, failed, bytes, queued: todo.length });
      }
      add(`Finished: ${copied} copied (${fmtBytes(bytes)}), ${failed} failed.` + (failed ? " Press Migrate again to retry the failed files." : ""),
          failed ? "warn" : "info");
    } catch (e) {
      add(e instanceof Error ? e.message : String(e), "error");
    } finally {
      setRunning(false);
    }
  }

  const processed = done.copied + done.failed;
  return (
    <>
      <div className="card">
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn" onClick={migrate} disabled={running}>{running ? "Migrating ..." : "Migrate"}</button>
          <button className="btn danger" onClick={() => { stopRef.current = true; add("Stop requested; finishing the current batch ...", "warn"); }}
                  disabled={!running}>Stop</button>
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={recopy} onChange={(e) => setRecopy(e.target.checked)} disabled={running} />
            Re-copy files already in Azure
          </label>
        </div>
        {plan && (
          <div className="stats" style={{ marginTop: 16 }}>
            <div className="stat"><span className="stat-label">Files in Vercel Blob</span><span className="stat-value">{plan.total}</span>
              <span className="tile-note">{fmtBytes(plan.bytes)} · {plan.folders} folders</span></div>
            <div className="stat"><span className="stat-label">Already in Azure</span><span className="stat-value">{plan.present}</span>
              <span className="tile-note">{plan.target}</span></div>
            <div className="stat"><span className="stat-label">Copied</span><span className="stat-value">{done.copied}</span>
              <span className="tile-note">{fmtBytes(done.bytes)}</span></div>
            <div className="stat"><span className="stat-label">Failed</span><span className="stat-value">{done.failed}</span></div>
            <div className="stat"><span className="stat-label">Remaining</span><span className="stat-value">{Math.max(0, done.queued - processed)}</span>
              <span className="tile-note">{done.queued ? `${Math.round((processed / done.queued) * 100)}% of this run` : ""}</span></div>
          </div>
        )}
      </div>
      <LiveLog title="Migration log" entries={log} running={running} />
      {failures.length > 0 && (
        <div className="card">
          <h2>Failed files ({failures.length})</h2>
          <ul>{failures.slice(0, 200).map((f) => <li key={f.pathname}><code>{f.pathname}</code> - {f.error}</li>)}</ul>
        </div>
      )}
    </>
  );
}
