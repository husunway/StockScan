"use client";

import { useEffect, useState } from "react";
import { api } from "./RunUi";

// Indexes the app can load, scan and backtest. Each has its own constituents table on the server.
export const INDEXES = [
  { id: "sp500", label: "S&P 500" },
  { id: "nasdaq100", label: "NASDAQ 100" },
] as const;
export type IndexId = (typeof INDEXES)[number]["id"];
export const indexLabel = (id: string | null | undefined) => INDEXES.find((i) => i.id === id)?.label ?? "S&P 500";

export function IndexSelect({ value, onChange, disabled, label = "Index" }: { value: IndexId; onChange: (v: IndexId) => void; disabled?: boolean; label?: string }) {
  return (
    <label>
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value as IndexId)} disabled={disabled} style={{ minWidth: 130 }}>
        {INDEXES.map((i) => <option key={i.id} value={i.id}>{i.label}</option>)}
      </select>
    </label>
  );
}

type ModelInfo = { id: string; default: boolean; price: { input: number; output: number } | null };
type Models = { default: string; provider: string | null; models: ModelInfo[]; currency: string };

let cache: Promise<Models> | null = null;
function loadModels(): Promise<Models> {
  if (!cache) cache = api<Models>("/api/py/agent/models").catch((e) => { cache = null; throw e; });
  return cache;
}

/** The configured models (DEFAULT_AI_MODEL plus AI_MODELS on the server). `value` "" means the server default. */
export function ModelSelect({ value, onChange, disabled }: { value: string; onChange: (v: string) => void; disabled?: boolean }) {
  const [models, setModels] = useState<Models | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    loadModels().then(setModels).catch((e: Error) => setError(e.message));
  }, []);
  const list = models?.models ?? [];
  const price = (m: ModelInfo) => (m.price ? ` · $${m.price.input}/$${m.price.output} per M tokens` : "");
  return (
    <label title={error ? `Could not load the model list: ${error}` : "Models come from DEFAULT_AI_MODEL (default) and AI_MODELS in the server environment"}>
      AI model
      <select value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled || list.length < 2} style={{ minWidth: 190 }}>
        {!models && <option value="">{error ? "server default" : "loading…"}</option>}
        {list.map((m) => (
          <option key={m.id} value={m.default ? "" : m.id}>{m.id}{m.default ? " (default)" : ""}{price(m)}</option>
        ))}
      </select>
    </label>
  );
}
