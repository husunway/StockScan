export type Point = { time: string; value: number };

export type Candle = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
};

export type VolumeBar = { time: string; value: number; up: boolean; ratio: number | null };

export type Crossover = { date: string; pair: string; type: "bullish" | "bearish"; price: number };

export type Level = {
  level: number;
  touches: number;
  last_date: string;
  type: "support" | "resistance";
};

export type Confirmation = {
  date: string;
  direction: "up" | "down";
  confirmed: boolean;
  return_pct: number;
  volume_ratio: number;
};

export type RelativeStrength = {
  benchmark: string;
  line: Point[];
  line_ma50: Point[];
  windows: Record<string, { stock: number; benchmark: number; excess: number }>;
  state: string;
  rs_20d_change: number | null;
} | null;

export type Analysis = {
  symbol: string;
  rows: number;
  start_date: string;
  end_date: string;
  candles: Candle[];
  moving_averages: Record<string, Point[]>;
  crossovers: Crossover[];
  rsi: Point[];
  macd: { macd: Point[]; signal: Point[]; histogram: Point[] };
  bollinger: { upper: Point[]; middle: Point[]; lower: Point[]; pct_b: Point[]; bandwidth: Point[] };
  volume: VolumeBar[];
  volume_ma20: Point[];
  volume_confirmations: Confirmation[];
  levels: Level[];
  relative_strength: { index?: RelativeStrength; sector?: RelativeStrength };
  signals: {
    last_close: number;
    change: number | null;
    change_pct: number | null;
    ma: {
      alignment: string;
      above: Record<string, boolean | null>;
      last_crossover: Crossover | null;
    };
    rsi: { value: number | null; state: string };
    macd: { macd: number | null; signal: number | null; histogram: number | null; state: string; momentum: string };
    bollinger: { pct_b: number | null; bandwidth: number | null; state: string; squeeze: boolean };
    volume: {
      trend: string;
      ma20_change_pct: number | null;
      last_ratio: number | null;
      last_confirmation: Confirmation | null;
    };
  };
  table: {
    date: string;
    close: number;
    ma20: number | null;
    ma50: number | null;
    ma200: number | null;
    rsi: number | null;
    macd: number | null;
    histogram: number | null;
    pct_b: number | null;
    volume_ratio: number | null;
  }[];
};

export const MA_PERIODS = ["5", "10", "20", "30", "50", "200"] as const;

/* Validated categorical palette (dataviz reference), one slot per MA in fixed order. */
export const MA_COLORS: Record<string, { light: string; dark: string }> = {
  "5": { light: "#2a78d6", dark: "#3987e5" },
  "10": { light: "#eb6834", dark: "#d95926" },
  "20": { light: "#1baf7a", dark: "#199e70" },
  "30": { light: "#eda100", dark: "#c98500" },
  "50": { light: "#e87ba4", dark: "#d55181" },
  "200": { light: "#008300", dark: "#008300" },
};

export const CHART_COLORS = {
  up: "#0ca30c",
  down: "#d03b3b",
  upSoft: "rgba(12, 163, 12, 0.45)",
  downSoft: "rgba(208, 59, 59, 0.45)",
  band: { light: "#898781", dark: "#898781" },
  bandFill: "rgba(137, 135, 129, 0.08)",
  rsi: { light: "#6250d6", dark: "#9085e9" },
  macd: { light: "#2a78d6", dark: "#3987e5" },
  signal: { light: "#eb6834", dark: "#d95926" },
  rsIndex: { light: "#2a78d6", dark: "#3987e5" },
  rsSector: { light: "#eb6834", dark: "#d95926" },
};
