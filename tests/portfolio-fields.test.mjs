// Run with: npm test   (Node 23.6+ loads the .ts module directly)
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validateField, validateAll, defaultInputs } from "../lib/portfolioValidation.ts";

const { fields } = JSON.parse(readFileSync(new URL("../data/portfolio_fields.json", import.meta.url), "utf8"));
const byKey = Object.fromEntries(fields.map((f) => [f.key, f]));

test("every field's default value passes its own validation", () => {
  for (const f of fields) {
    const r = validateField(f, String(f.default));
    assert.equal(r.ok, true, `${f.key} default ${f.default} rejected: ${r.error}`);
    assert.equal(r.value, f.default);
  }
  const all = validateAll(fields, defaultInputs(fields));
  assert.equal(all.ok, true, JSON.stringify(all.errors));
});

test("every default lies inside its own min and max", () => {
  for (const f of fields) {
    assert.ok(f.default >= f.min && f.default <= f.max, f.key);
    if (f.kind === "integer") assert.ok(Number.isInteger(f.default), f.key);
  }
});

test("whole dollar amounts are accepted, with or without separators", () => {
  for (const key of ["starting_capital", "buy_amount"]) {
    for (const [raw, want] of [["10000000", 10000000], ["10,000,000", 10000000], ["12345678", 12345678], ["12,345,678", 12345678], ["5000", 5000], ["$5,000", 5000]]) {
      const r = validateField(byKey[key], raw);
      assert.equal(r.ok, true, `${key} ${raw}: ${r.error}`);
      assert.equal(r.value, want);
    }
  }
});

test("percentage fields accept whole numbers and decimals", () => {
  for (const key of ["take_profit_pct", "rerun_proximity_pct", "skip_threshold_pct"]) {
    for (const [raw, want] of [["10", 10], ["7.5", 7.5], ["0.25", 0.25], ["12.5%", 12.5]]) {
      const r = validateField(byKey[key], raw);
      assert.equal(r.ok, true, `${key} ${raw}: ${r.error}`);
      assert.equal(r.value, want);
    }
  }
});

test("invalid input is rejected with a specific message", () => {
  const cap = byKey.starting_capital;
  assert.match(validateField(cap, "").error, /Starting capital is required/);
  assert.match(validateField(cap, "abc").error, /must be a whole dollar amount/);
  assert.match(validateField(cap, "-5000").error, /between \$1,000 and/);
  assert.match(validateField(cap, "0").error, /between/);
  assert.match(validateField(cap, "10000.5").error, /without decimals/);
  assert.match(validateField(byKey.stock_count, "2.5").error, /whole number/);
  assert.match(validateField(byKey.take_profit_pct, "0").error, /between 0.1% and/);
  assert.match(validateField(byKey.held_reassess_days, "0").error, /between 1 trading day and 60 trading days/);
  assert.equal(validateAll(fields, { ...defaultInputs(fields), buy_amount: "" }).ok, false);
});
