"use client";

import { useEffect, useRef, useState } from "react";
import {
  createChart,
  createSeriesMarkers,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type SeriesMarker,
  type SeriesType,
  type Time,
} from "lightweight-charts";
import { type Analysis, CHART_COLORS, MA_COLORS, MA_PERIODS } from "./analysisTypes";

export type Range = "3m" | "6m" | "1y" | "all";
const RANGE_BARS: Record<Range, number> = { "3m": 63, "6m": 126, "1y": 252, all: 0 };

type Props = { data: Analysis; visibleMAs: Set<string>; dark: boolean; range: Range };

type Tracked = { key: string; series: ISeriesApi<SeriesType>; fmt: (v: number) => string; color: string };
type Readout = { time: string; items: { key: string; value: string; color: string }[] };

const f2 = (v: number) => v.toFixed(2);
const f1 = (v: number) => v.toFixed(1);
const fVol = (v: number) => (v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}K` : `${v}`);

function valueOf(d: unknown): number | undefined {
  if (!d || typeof d !== "object") return undefined;
  const o = d as { value?: number; close?: number };
  return o.value ?? o.close;
}

export default function StockChart({ data, visibleMAs, dark, range }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const [hover, setHover] = useState<Readout | null>(null);
  const [latest, setLatest] = useState<Readout | null>(null);
  const mode = dark ? "dark" : "light";

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const css = getComputedStyle(document.documentElement);
    const v = (name: string) => css.getPropertyValue(name).trim();
    const muted = v("--muted");
    const border = v("--border");

    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: v("--surface") },
        textColor: muted,
        panes: { separatorColor: border, separatorHoverColor: border, enableResize: false },
      },
      grid: { vertLines: { color: border }, horzLines: { color: border } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: border },
      timeScale: { borderColor: border, rightOffset: 4 },
    });
    chartRef.current = chart;
    const tracked: Tracked[] = [];
    const hidden = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };

    // Pane 0: candles, Bollinger Bands, moving averages, support/resistance
    const candles = chart.addSeries(
      CandlestickSeries,
      {
        upColor: CHART_COLORS.up,
        downColor: CHART_COLORS.down,
        wickUpColor: CHART_COLORS.up,
        wickDownColor: CHART_COLORS.down,
        borderVisible: false,
      },
      0
    );
    candles.setData(data.candles as { time: Time; open: number; high: number; low: number; close: number }[]);

    const bandColor = CHART_COLORS.band[mode];
    const bbOpts = { color: bandColor, lineWidth: 1 as const, lineStyle: LineStyle.Dotted, ...hidden };
    const bbUpper = chart.addSeries(LineSeries, bbOpts, 0);
    const bbLower = chart.addSeries(LineSeries, bbOpts, 0);
    bbUpper.setData(data.bollinger.upper as { time: Time; value: number }[]);
    bbLower.setData(data.bollinger.lower as { time: Time; value: number }[]);
    tracked.push({ key: "BB upper", series: bbUpper, fmt: f2, color: bandColor });
    tracked.push({ key: "BB lower", series: bbLower, fmt: f2, color: bandColor });

    for (const p of MA_PERIODS) {
      if (!visibleMAs.has(p)) continue;
      const color = MA_COLORS[p][mode];
      const s = chart.addSeries(LineSeries, { color, lineWidth: 2, ...hidden }, 0);
      s.setData(data.moving_averages[p] as { time: Time; value: number }[]);
      tracked.push({ key: `MA${p}`, series: s, fmt: f2, color });
    }

    for (const l of data.levels) {
      const support = l.type === "support";
      candles.createPriceLine({
        price: l.level,
        color: support ? CHART_COLORS.up : CHART_COLORS.down,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: support ? `S ×${l.touches}` : `R ×${l.touches}`,
      });
    }

    const crossMarkers: SeriesMarker<Time>[] = data.crossovers
      .filter((c) => c.pair.replace("MA", "").split("/").every((p) => visibleMAs.has(p.replace("MA", ""))))
      .map((c) => ({
        time: c.date as Time,
        position: c.type === "bullish" ? "belowBar" : "aboveBar",
        shape: c.type === "bullish" ? "arrowUp" : "arrowDown",
        color: c.type === "bullish" ? CHART_COLORS.up : CHART_COLORS.down,
        text: c.pair.replace(/MA/g, ""),
      }));
    createSeriesMarkers(candles, crossMarkers);

    // Pane 1: volume with 20-day average and confirmation markers
    const volume = chart.addSeries(
      HistogramSeries,
      { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false },
      1
    );
    volume.setData(
      data.volume.map((b) => ({
        time: b.time as Time,
        value: b.value,
        color: b.up ? CHART_COLORS.upSoft : CHART_COLORS.downSoft,
      }))
    );
    const volMa = chart.addSeries(LineSeries, { color: muted, lineWidth: 1, ...hidden }, 1);
    volMa.setData(data.volume_ma20 as { time: Time; value: number }[]);
    tracked.push({ key: "Volume", series: volume, fmt: fVol, color: muted });
    tracked.push({ key: "Vol MA20", series: volMa, fmt: fVol, color: muted });
    createSeriesMarkers(
      volume,
      data.volume_confirmations.map((c) => ({
        time: c.date as Time,
        position: "aboveBar" as const,
        shape: "circle" as const,
        color: c.confirmed ? (c.direction === "up" ? CHART_COLORS.up : CHART_COLORS.down) : muted,
        text: c.confirmed ? "✓ vol" : "weak",
        size: 0.6,
      }))
    );

    // Pane 2: RSI with 70 / 30 bands
    const rsi = chart.addSeries(
      LineSeries,
      {
        color: CHART_COLORS.rsi[mode],
        lineWidth: 2,
        priceLineVisible: false,
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
      },
      2
    );
    rsi.setData(data.rsi as { time: Time; value: number }[]);
    for (const [price, title] of [[70, "70"], [30, "30"]] as const) {
      rsi.createPriceLine({ price, color: muted, lineWidth: 1, lineStyle: LineStyle.Dashed, title, axisLabelVisible: false });
    }
    tracked.push({ key: "RSI", series: rsi, fmt: f1, color: CHART_COLORS.rsi[mode] });

    // Pane 3: MACD
    const hist = chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, 3);
    hist.setData(
      data.macd.histogram.map((p) => ({
        time: p.time as Time,
        value: p.value,
        color: p.value >= 0 ? CHART_COLORS.upSoft : CHART_COLORS.downSoft,
      }))
    );
    const macd = chart.addSeries(LineSeries, { color: CHART_COLORS.macd[mode], lineWidth: 2, ...hidden }, 3);
    const signal = chart.addSeries(LineSeries, { color: CHART_COLORS.signal[mode], lineWidth: 2, ...hidden }, 3);
    macd.setData(data.macd.macd as { time: Time; value: number }[]);
    signal.setData(data.macd.signal as { time: Time; value: number }[]);
    tracked.push({ key: "MACD", series: macd, fmt: f2, color: CHART_COLORS.macd[mode] });
    tracked.push({ key: "Signal", series: signal, fmt: f2, color: CHART_COLORS.signal[mode] });
    tracked.push({ key: "Hist", series: hist, fmt: f2, color: muted });

    // Pane 4: relative strength (stock / benchmark, rebased to 100)
    const rsEntries = [
      ["index", data.relative_strength.index, CHART_COLORS.rsIndex[mode]],
      ["sector", data.relative_strength.sector, CHART_COLORS.rsSector[mode]],
    ] as const;
    let rsPane = false;
    for (const [, rs, color] of rsEntries) {
      if (!rs) continue;
      const line = chart.addSeries(LineSeries, { color, lineWidth: 2, ...hidden }, 4);
      line.setData(rs.line as { time: Time; value: number }[]);
      const ma = chart.addSeries(LineSeries, { color, lineWidth: 1, lineStyle: LineStyle.Dotted, ...hidden }, 4);
      ma.setData(rs.line_ma50 as { time: Time; value: number }[]);
      if (!rsPane) {
        line.createPriceLine({ price: 100, color: muted, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false, title: "" });
        rsPane = true;
      }
      tracked.push({ key: `RS vs ${rs.benchmark}`, series: line, fmt: f1, color });
    }

    const factors = [3, 0.8, 0.9, 1, 1];
    chart.panes().forEach((pane, i) => pane.setStretchFactor(factors[i] ?? 1));

    // Crosshair readout: every series at the hovered date
    const onMove = (param: MouseEventParams<Time>) => {
      if (!param.time) {
        setHover(null);
        return;
      }
      const c = param.seriesData.get(candles) as { open: number; high: number; low: number; close: number } | undefined;
      const items = c
        ? [
            { key: "O", value: f2(c.open), color: "" },
            { key: "H", value: f2(c.high), color: "" },
            { key: "L", value: f2(c.low), color: "" },
            { key: "C", value: f2(c.close), color: c.close >= c.open ? CHART_COLORS.up : CHART_COLORS.down },
          ]
        : [];
      for (const t of tracked) {
        const val = valueOf(param.seriesData.get(t.series));
        if (val !== undefined) items.push({ key: t.key, value: t.fmt(val), color: t.color });
      }
      setHover({ time: String(param.time), items });
    };
    chart.subscribeCrosshairMove(onMove);

    // Latest values, shown when nothing is hovered
    const last = data.candles[data.candles.length - 1];
    const lastItems = [
      { key: "O", value: f2(last.open), color: "" },
      { key: "H", value: f2(last.high), color: "" },
      { key: "L", value: f2(last.low), color: "" },
      { key: "C", value: f2(last.close), color: last.close >= last.open ? CHART_COLORS.up : CHART_COLORS.down },
    ];
    for (const t of tracked) {
      const d = t.series.data();
      const val = valueOf(d[d.length - 1]);
      if (val !== undefined) lastItems.push({ key: t.key, value: t.fmt(val), color: t.color });
    }
    setLatest({ time: last.time, items: lastItems });

    applyRange(chart, data.candles.length, range);

    return () => {
      chart.unsubscribeCrosshairMove(onMove);
      chart.remove();
      chartRef.current = null;
    };
    // range is applied in its own effect; rebuilding on it would flash the chart
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, visibleMAs, mode]);

  useEffect(() => {
    if (chartRef.current) applyRange(chartRef.current, data.candles.length, range);
  }, [range, data.candles.length]);

  const readout = hover ?? latest;
  const paneCount = data.relative_strength.index || data.relative_strength.sector ? 5 : 4;

  return (
    <div className="chart-block">
      <div className="readout" aria-live="polite">
        {readout && (
          <>
            <span className="readout-time">{readout.time}{hover ? "" : " (latest)"}</span>
            {readout.items.map((it) => (
              <span key={it.key} className="readout-item">
                {it.color && <i className="key" style={{ background: it.color }} />}
                <span className="readout-label">{it.key}</span>
                <strong>{it.value}</strong>
              </span>
            ))}
          </>
        )}
      </div>
      <div ref={ref} className="chart" style={{ height: paneCount === 5 ? 860 : 720 }} />
    </div>
  );
}

function applyRange(chart: IChartApi, bars: number, range: Range) {
  const n = RANGE_BARS[range];
  if (!n || n >= bars) chart.timeScale().fitContent();
  else chart.timeScale().setVisibleLogicalRange({ from: bars - n, to: bars + 3 });
}
