/**
 * Validation for the portfolio backtest form. The rules come from data/portfolio_fields.json, which the
 * API (validate_portfolio_config in api/index.py) reads too, so the browser and the server cannot disagree.
 * Kept free of imports so `node --test` can load it directly.
 */

export type FieldSpec = {
  key: string;
  label: string;
  kind: "integer" | "decimal";
  min: number;
  max: number;
  default: number;
  unit: "count" | "usd" | "pct" | "days" | "trading days";
};

export type FieldResult = { ok: true; value: number } | { ok: false; error: string };

const NUMBER_RE = /^-?(\d+(\.\d*)?|\.\d+)$/;

function fmt(spec: FieldSpec, v: number): string {
  const s = v.toLocaleString("en-CA", { maximumFractionDigits: 2 });
  if (spec.unit === "usd") return `$${s}`;
  if (spec.unit === "pct") return `${s}%`;
  if (spec.unit === "days" || spec.unit === "trading days") return `${s} ${v === 1 ? spec.unit.slice(0, -1) : spec.unit}`;
  return s;
}

function noun(spec: FieldSpec): string {
  if (spec.kind === "decimal") return "a number";
  return spec.unit === "usd" ? "a whole dollar amount" : "a whole number";
}

/** Parse what the user typed: tolerates thousands separators, spaces, a leading $ and a trailing %. */
export function parseFieldInput(raw: string): string {
  return raw.trim().replace(/[\s,_  ]/g, "").replace(/^\$/, "").replace(/%$/, "");
}

export function validateField(spec: FieldSpec, raw: string | number | null | undefined): FieldResult {
  const text = raw === null || raw === undefined ? "" : parseFieldInput(String(raw));
  const range = `between ${fmt(spec, spec.min)} and ${fmt(spec, spec.max)}`;
  if (text === "") return { ok: false, error: `${spec.label} is required.` };
  if (!NUMBER_RE.test(text)) return { ok: false, error: `${spec.label} must be ${noun(spec)} (for example ${fmt(spec, spec.default)}).` };
  const value = Number(text);
  if (!Number.isFinite(value)) return { ok: false, error: `${spec.label} must be ${noun(spec)}.` };
  if (spec.kind === "integer" && !Number.isInteger(value)) return { ok: false, error: `${spec.label} must be ${noun(spec)}, without decimals.` };
  if (value < spec.min || value > spec.max) return { ok: false, error: `${spec.label} must be ${noun(spec)} ${range}.` };
  return { ok: true, value };
}

/** Validate every field; returns the parsed values and a message per invalid field. */
export function validateAll(specs: FieldSpec[], raw: Record<string, string | number>) {
  const values: Record<string, number> = {};
  const errors: Record<string, string> = {};
  for (const spec of specs) {
    const r = validateField(spec, raw[spec.key]);
    if (r.ok) values[spec.key] = r.value;
    else errors[spec.key] = r.error;
  }
  return { values, errors, ok: Object.keys(errors).length === 0 };
}

export function defaultInputs(specs: FieldSpec[]): Record<string, string> {
  return Object.fromEntries(specs.map((s) => [s.key, String(s.default)]));
}
