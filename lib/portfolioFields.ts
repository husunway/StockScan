import spec from "@/data/portfolio_fields.json";
import type { FieldSpec } from "./portfolioValidation";

export const PORTFOLIO_FIELDS = spec.fields as FieldSpec[];
export const FIELD_BY_KEY: Record<string, FieldSpec> = Object.fromEntries(PORTFOLIO_FIELDS.map((f) => [f.key, f]));
