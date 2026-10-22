"use client";

import { useEffect, useRef, useState } from "react";

export type LogEntry = { t: string; elapsed_s: number; level: "info" | "warn" | "error"; message: string };

export type Diagnostics = {
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

export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`);
  return data as T;
}

type Hello = { model: string; reply: string; latency_s: number; input_tokens: number; output_tokens: number; log: LogEntry[] };

/** Configuration the server will use (provider, database, Blob, runtime) with a live DB check and a Test AI button. */
export function DiagnosticsPanel({ diag, error, onLog, showAi = true }: { diag: Diagnostics | null; error: string | null; onLog: (entries: LogEntry[]) => void; showAi?: boolean }) {
  const [open, setOpen] = useState(true);
  const [hello, setHello] = useState<Hello | null>(null);
  const [helloError, setHelloError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  if (error) return <p className="msg error">Diagnostics unavailable: {error}</p>;
  if (!diag) return <p className="msg">Checking configuration…</p>;
  const db = diag.database;
  const dot = (ok: boolean | undefined) => <span className={`dot ${ok === undefined ? "" : ok ? "ok" : "down"}`} />;

  async function testAi() {
    setTesting(true);
    setHello(null);
    setHelloError(null);
    try {
      const r = await api<Hello>("/api/py/agent/hello", { method: "POST" });
      setHello(r);
      onLog(r.log);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "AI test failed";
      setHelloError(msg);
      onLog([{ t: new Date().toISOString(), elapsed_s: 0, level: "error", message: msg }]);
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="diag">
      <button type="button" className="linklike diag-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? "▾" : "▸"} Configuration used for this run
      </button>
      {open && (
        <div className="diag-grid">
          {showAi && (
            <div className="diag-item">
              <span className="stat-label">{dot(hello ? true : helloError ? false : diag.claude.provider !== "not configured")} Claude</span>
              <strong>{diag.claude.provider}</strong>
              <span>{diag.claude.endpoint ?? "—"}</span>
              <span>model <code>{diag.claude.model ?? "—"}</code>{diag.claude.effort && <> · effort {diag.claude.effort}</>}</span>
              <span className="tile-note">{diag.claude.variables.join(" · ")}</span>
              <span>
                <button type="button" className="btn small ghost" disabled={testing || diag.claude.provider === "not configured"} onClick={testAi}>
                  {testing ? "Saying hello…" : "Test AI"}
                </button>
              </span>
              {hello && (
                <span className="hello-reply">
                  <strong>{hello.model}</strong> replied in {hello.latency_s}s: &ldquo;{hello.reply}&rdquo;
                </span>
              )}
              {helloError && <span className="verdict-fail">{helloError}</span>}
            </div>
          )}
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

/** Console-style log that follows new entries and shows how long it has been waiting while running. */
export function LiveLog({ title, entries, running, waitHint }: { title: string; entries: LogEntry[]; running: boolean; waitHint?: string }) {
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
                ? "No update for over 150 s. The server function may have been terminated (Vercel maxDuration per step, 300 s in vercel.json). Use Stop run, then Retry failed; consider ANTHROPIC_EFFORT=medium."
                : waitHint ?? "waiting for the next update (progress lines appear every ~15 s while the model generates)"}
            </span>
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}
