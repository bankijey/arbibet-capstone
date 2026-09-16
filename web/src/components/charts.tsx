"use client";

import { BOOK_DASHED, BOOK_OUTLINED, MAX_LEG_SPREAD_SECONDS, bookColour } from "@/lib/betting";
import { clock, kickoff } from "@/lib/format";
import type { Point } from "@/lib/types";

import { type ChartTheme, EChart, axisStyle, tooltipStyle } from "./EChart";

const DETECTED = "#e11d48";
const LINE = "#3b82f6";
const STALE = "#94a3b8";

const iso = (value: number | string) => new Date(value).toISOString();
const fmtTime = (value: number | string) => kickoff(iso(value));

function timeAxis(theme: ChartTheme) {
  return {
    type: "time" as const,
    ...axisStyle(theme),
    splitLine: { show: false },
    axisLabel: {
      color: theme.muted,
      fontSize: 11,
      hideOverlap: true,
      formatter: (v: number) => `${kickoff(iso(v)).split(" ").slice(0, 2).join(" ")}
${clock(iso(v))}`,
    },
  };
}

function kickoffLine(kickoffAt: string | null | undefined, theme: ChartTheme) {
  if (!kickoffAt) return undefined;
  return {
    symbol: "none",
    silent: true,
    lineStyle: { color: theme.muted, type: "dashed" as const, width: 1 },
    label: { formatter: "kick-off", color: theme.muted, fontSize: 11, position: "insideEndTop" as const },
    data: [{ xAxis: new Date(kickoffAt).getTime() }],
  };
}

/** A price holds until changed: carry each step line forward to `end`. */
function extend(points: Point[], end: number): [number, number][] {
  const out = points
    .filter((p) => p[1] !== null)
    .map((p) => [new Date(p[0]).getTime(), p[1] as number] as [number, number]);
  if (out.length && end > out[out.length - 1][0]) out.push([end, out[out.length - 1][1]]);
  return out;
}

export function ArbitrageChart({
  points,
  detections,
  kickoffAt,
  nowLabel,
  height = 280,
}: {
  points: Point[];
  detections: { at: string; arbitrage: number }[];
  kickoffAt: string | null;
  nowLabel: string;
  height?: number;
}) {
  return (
    <EChart
      height={height}
      deps={[points, detections, kickoffAt, nowLabel]}
      build={(theme) => {
        const line = points.filter((p) => p[1] !== null);
        const last = line[line.length - 1];
        const values = [...line.map((p) => p[1] as number), ...detections.map((d) => d.arbitrage), 1];
        const min = Math.min(...values);
        const max = Math.max(...values);
        const pad = Math.max((max - min) * 0.15, 0.005);
        return {
          grid: { left: 8, right: 16, top: 28, bottom: 8, outerBoundsMode: "same" as const },
          legend: { top: 0, left: 0, textStyle: { color: theme.muted, fontSize: 12 }, itemHeight: 10 },
          tooltip: {
            trigger: "item",
            ...tooltipStyle(theme),
            formatter: (p: { seriesName: string; value: [number, number, number?] }) =>
              `${fmtTime(p.value[0])}<br/><b>${p.value[1].toFixed(4)}</b> ${p.seriesName}` +
              (p.value[2] !== undefined ? `<br/>legs ${(p.value[2] / 60).toFixed(1)} min apart` : ""),
          },
          xAxis: timeAxis(theme),
          yAxis: {
            type: "value",
            min: +(min - pad).toFixed(3),
            max: +(max + pad).toFixed(3),
            ...axisStyle(theme),
            axisLabel: { color: theme.muted, fontSize: 11, formatter: (v: number) => v.toFixed(3) },
          },
          series: [
            {
              name: "arbitrage",
              type: "line",
              step: "end",
              symbolSize: 6,
              data: line.map((p) => ({
                value: [new Date(p[0]).getTime(), p[1], p[2] ?? 0],
                itemStyle: { color: (p[2] ?? 1e9) <= MAX_LEG_SPREAD_SECONDS ? LINE : STALE },
              })),
              lineStyle: { color: LINE, width: 2 },
              itemStyle: { color: LINE },
              markLine: {
                symbol: "none",
                silent: true,
                data: [
                  { yAxis: 1, lineStyle: { color: theme.muted, type: "dotted" as const }, label: { formatter: "1.0 surebet", color: theme.muted, fontSize: 11, position: "insideEndBottom" as const } },
                  ...(kickoffAt
                    ? [
                        {
                          xAxis: new Date(kickoffAt).getTime(),
                          lineStyle: { color: theme.muted, type: "dashed" as const },
                          label: { formatter: "kick-off", color: theme.muted, fontSize: 11 },
                        },
                      ]
                    : []),
                ],
              },
            },
            {
              name: "surebet detected",
              type: "scatter",
              symbol: "diamond",
              symbolSize: 13,
              itemStyle: { color: DETECTED, borderColor: theme.surface, borderWidth: 1 },
              data: detections.map((d) => [new Date(d.at).getTime(), d.arbitrage]),
              z: 5,
            },
            ...(last
              ? [
                  {
                    name: nowLabel,
                    type: "scatter",
                    symbol: "path://M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z",
                    symbolSize: 16,
                    itemStyle: { color: theme.text },
                    data: [[new Date(last[0]).getTime(), last[1]]],
                    z: 6,
                  },
                ]
              : []),
          ],
        };
      }}
    />
  );
}

export function OddsChart({
  prices,
  highlight,
  marks,
  kickoffAt,
  end,
  markLabel,
  height = 340,
}: {
  prices: Record<string, Point[]>;
  highlight: string | null;
  marks: { at: string; odds: number; ev: number; book: string }[];
  kickoffAt: string | null;
  end: number;
  markLabel: string;
  height?: number;
}) {
  return (
    <EChart
      height={height}
      deps={[prices, highlight, marks, kickoffAt, end, markLabel]}
      build={(theme) => {
        const books = Object.keys(prices).sort();
        const lines = books.flatMap((book) => {
          const data = extend(prices[book], end);
          const emphasis = book === highlight;
          const dim = highlight !== null && !emphasis;
          const width = emphasis ? 3.5 : 2;
          const base = {
            type: "line" as const,
            step: "end" as const,
            showSymbol: false,
            data,
          };
          const series = [];
          if (BOOK_OUTLINED.has(book)) {
            series.push({
              ...base,
              name: `${book}-outline`,
              lineStyle: { color: theme.dark ? "#000" : "#1f2937", width: width + 2.5, opacity: dim ? 0.4 : 1 },
              silent: true,
              tooltip: { show: false },
              legendHoverLink: false,
            });
          }
          series.push({
            ...base,
            name: book,
            showSymbol: true,
            symbolSize: 5,
            lineStyle: {
              color: bookColour(book),
              width,
              type: BOOK_DASHED.has(book) ? ("dashed" as const) : ("solid" as const),
              opacity: dim ? 0.45 : 1,
            },
            itemStyle: { color: bookColour(book), opacity: dim ? 0.45 : 1 },
            z: emphasis ? 4 : 2,
          });
          return series;
        });
        const mine = highlight ? prices[highlight] : undefined;
        const lastMine = mine?.filter((p) => p[1] !== null).at(-1);
        return {
          grid: { left: 8, right: 16, top: 30, bottom: 8, outerBoundsMode: "same" as const },
          legend: {
            top: 0,
            left: 0,
            data: [...books, ...(marks.length ? [markLabel] : []), ...(lastMine ? ["last recorded odds"] : [])],
            textStyle: { color: theme.muted, fontSize: 12 },
            itemHeight: 10,
          },
          tooltip: {
            trigger: "item",
            ...tooltipStyle(theme),
            formatter: (p: { seriesName: string; value: [number, number]; data: { ev?: number; book?: string } }) =>
              p.data?.ev !== undefined
                ? `${fmtTime(p.value[0])}<br/>EV <b>${p.data.ev.toFixed(3)}</b> at ${p.data.book}<br/>odds ${p.value[1].toFixed(2)}`
                : `${fmtTime(p.value[0])}<br/>${p.seriesName} <b>${p.value[1].toFixed(2)}</b>`,
          },
          xAxis: { ...timeAxis(theme), max: end },
          yAxis: { type: "value", scale: true, ...axisStyle(theme) },
          series: [
            ...lines,
            {
              name: markLabel,
              type: "scatter",
              symbol: "diamond",
              symbolSize: 14,
              itemStyle: { color: DETECTED, borderColor: theme.surface, borderWidth: 1 },
              data: marks.map((m) => ({ value: [new Date(m.at).getTime(), m.odds], ev: m.ev, book: m.book })),
              z: 6,
              markLine: kickoffLine(kickoffAt, theme),
            },
            ...(lastMine
              ? [
                  {
                    name: "last recorded odds",
                    type: "scatter",
                    symbol: "path://M12 2l3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z",
                    symbolSize: 16,
                    itemStyle: { color: theme.text },
                    data: [[Math.max(end, new Date(lastMine[0]).getTime()), lastMine[1]]],
                    z: 7,
                  },
                ]
              : []),
          ],
        };
      }}
    />
  );
}

/** One outcome's price at every book, for the deep dive's market panels. */
export function MarketPanel({
  title,
  prices,
  kickoffAt,
  height = 220,
}: {
  title: string;
  prices: Record<string, Point[]>;
  kickoffAt: string;
  height?: number;
}) {
  return (
    <div>
      <div className="mb-1 text-sm font-medium">{title}</div>
      <EChart
        height={height}
        deps={[prices, kickoffAt]}
        build={(theme) => {
          const books = Object.keys(prices).sort();
          const allX = books.flatMap((b) => prices[b].map((p) => new Date(p[0]).getTime()));
          const end = Math.max(...allX, new Date(kickoffAt).getTime());
          return {
            grid: { left: 8, right: 16, top: 26, bottom: 8, outerBoundsMode: "same" as const },
            legend: { top: 0, left: 0, textStyle: { color: theme.muted, fontSize: 11 }, itemHeight: 8 },
            tooltip: { trigger: "axis", ...tooltipStyle(theme), valueFormatter: (v: number) => v?.toFixed(2) },
            xAxis: timeAxis(theme),
            yAxis: { type: "value", scale: true, ...axisStyle(theme) },
            dataZoom: [{ type: "inside", zoomOnMouseWheel: "ctrl", moveOnMouseWheel: false }],
            series: books.map((book, i) => ({
              name: book,
              type: "line",
              step: "end",
              showSymbol: false,
              data: extend(prices[book], end),
              lineStyle: {
                color: bookColour(book),
                width: 2,
                type: BOOK_DASHED.has(book) ? "dashed" : "solid",
              },
              itemStyle: { color: bookColour(book) },
              markLine: i === 0 ? kickoffLine(kickoffAt, theme) : undefined,
            })),
          };
        }}
      />
    </div>
  );
}

export function BookBars({ rows, label }: { rows: { book: string; value: number }[]; label: string }) {
  return (
    <EChart
      height={260}
      deps={[rows, label]}
      build={(theme) => ({
        grid: { left: 8, right: 16, top: 16, bottom: 8, outerBoundsMode: "same" as const },
        tooltip: { trigger: "item", ...tooltipStyle(theme) },
        xAxis: { type: "category", data: rows.map((r) => r.book), ...axisStyle(theme), splitLine: { show: false } },
        yAxis: { type: "value", name: label, nameTextStyle: { color: theme.muted, fontSize: 11 }, ...axisStyle(theme) },
        series: [
          {
            type: "bar",
            barMaxWidth: 56,
            data: rows.map((r) => ({
              value: r.value,
              itemStyle: {
                color: bookColour(r.book),
                borderColor: BOOK_OUTLINED.has(r.book) ? "#111" : "transparent",
                borderWidth: BOOK_OUTLINED.has(r.book) ? 2 : 0,
                borderRadius: [4, 4, 0, 0],
              },
            })),
          },
        ],
      })}
    />
  );
}

export function EfficiencyBars({ rows }: { rows: { label: string; signals: number; meanOverround: number }[] }) {
  const ordered = [...rows].sort((a, b) => b.meanOverround - a.meanOverround);
  return (
    <EChart
      height={Math.max(260, 22 * ordered.length + 40)}
      deps={[rows]}
      build={(theme) => ({
        grid: { left: 8, right: 24, top: 8, bottom: 8, outerBoundsMode: "same" as const },
        tooltip: {
          trigger: "item",
          ...tooltipStyle(theme),
          formatter: (p: { name: string; value: number; dataIndex: number }) =>
            `${p.name}<br/>mean overround <b>${(p.value * 100).toFixed(2)}%</b><br/>${ordered[p.dataIndex].signals} signals`,
        },
        xAxis: {
          type: "value",
          ...axisStyle(theme),
          axisLabel: { color: theme.muted, fontSize: 11, formatter: (v: number) => `${(v * 100).toFixed(1)}%` },
        },
        yAxis: {
          type: "category",
          data: ordered.map((r) => r.label),
          ...axisStyle(theme),
          splitLine: { show: false },
          axisLabel: { color: theme.muted, fontSize: 11, width: 220, overflow: "truncate" },
        },
        series: [
          {
            type: "bar",
            barMaxWidth: 14,
            data: ordered.map((r) => ({
              value: r.meanOverround,
              itemStyle: { color: r.meanOverround < 0 ? "#dc2626" : "#3b82f6", borderRadius: 3 },
            })),
          },
        ],
      })}
    />
  );
}

export function BacktestChart({ curves, labels }: { curves: Record<string, Point[]>; labels: Record<string, string> }) {
  const palette = ["#64748b", "#0ea5e9", "#e11d48", "#8b5cf6", "#10b981"];
  return (
    <EChart
      height={320}
      deps={[curves]}
      build={(theme) => ({
        grid: { left: 8, right: 16, top: 34, bottom: 8, outerBoundsMode: "same" as const },
        legend: { top: 0, left: 0, textStyle: { color: theme.muted, fontSize: 12 }, itemHeight: 10 },
        tooltip: { trigger: "axis", ...tooltipStyle(theme), valueFormatter: (v: number) => v?.toFixed(1) },
        xAxis: timeAxis(theme),
        yAxis: { type: "value", scale: true, ...axisStyle(theme) },
        series: Object.entries(curves).map(([sizing, points], i) => ({
          name: labels[sizing] ?? sizing,
          type: "line",
          step: "end",
          showSymbol: false,
          data: [...points.map((p) => [new Date(p[0]).getTime(), p[1]])],
          lineStyle: { width: 2, color: palette[i % palette.length] },
          itemStyle: { color: palette[i % palette.length] },
          markLine:
            i === 0
              ? {
                  symbol: "none",
                  silent: true,
                  lineStyle: { color: theme.muted, type: "dotted" },
                  label: { formatter: "start 100", color: theme.muted, fontSize: 11 },
                  data: [{ yAxis: 100 }],
                }
              : undefined,
        })),
      })}
    />
  );
}

export function SpreadBars({ rows }: { rows: { spread: string; signals: number; surebets: number }[] }) {
  return (
    <EChart
      height={220}
      deps={[rows]}
      build={(theme) => ({
        grid: { left: 8, right: 16, top: 28, bottom: 8, outerBoundsMode: "same" as const },
        legend: { top: 0, left: 0, textStyle: { color: theme.muted, fontSize: 12 }, itemHeight: 10 },
        tooltip: { trigger: "axis", ...tooltipStyle(theme) },
        xAxis: { type: "category", data: rows.map((r) => r.spread), ...axisStyle(theme), splitLine: { show: false } },
        yAxis: { type: "value", ...axisStyle(theme) },
        series: [
          { name: "signals", type: "bar", data: rows.map((r) => r.signals), itemStyle: { color: "#3b82f6", borderRadius: [4, 4, 0, 0] }, barMaxWidth: 40 },
          { name: "surebets", type: "bar", data: rows.map((r) => r.surebets), itemStyle: { color: DETECTED, borderRadius: [4, 4, 0, 0] }, barMaxWidth: 40 },
        ],
      })}
    />
  );
}
