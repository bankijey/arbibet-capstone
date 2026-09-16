"use client";

import { BarChart, LineChart, ScatterChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

import { useDark } from "@/lib/stores";

// Only the pieces the site draws, so the bundle carries a fraction of ECharts.
echarts.use([
  LineChart,
  BarChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
  DataZoomComponent,
  CanvasRenderer,
]);

export interface ChartTheme {
  dark: boolean;
  text: string;
  muted: string;
  grid: string;
  surface: string;
}

const LIGHT: ChartTheme = { dark: false, text: "#0f172a", muted: "#5b6474", grid: "#e3e6eb", surface: "#ffffff" };
const DARK: ChartTheme = { dark: true, text: "#e6e9ef", muted: "#9aa4b5", grid: "#262f40", surface: "#121824" };

export function EChart({
  build,
  height = 320,
  deps,
}: {
  build: (theme: ChartTheme) => EChartsCoreOption;
  height?: number;
  deps: unknown[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const theme = useDark() ? DARK : LIGHT;

  useEffect(() => {
    if (!ref.current) return;
    const instance = echarts.init(ref.current, undefined, { renderer: "canvas" });
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    if (!chart.current) return;
    chart.current.setOption(
      {
        backgroundColor: "transparent",
        textStyle: { fontFamily: "var(--font-geist-sans), system-ui, sans-serif", color: theme.text },
        animationDuration: 300,
        ...build(theme),
      },
      { notMerge: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, ...deps]);

  // overflow hidden: a canvas drawn at the old width must not hold its box
  // open when the viewport narrows, or the ResizeObserver never sees it shrink.
  return <div ref={ref} style={{ height, width: "100%", minWidth: 0, overflow: "hidden" }} />;
}

export const axisStyle = (theme: ChartTheme) => ({
  axisLine: { lineStyle: { color: theme.grid } },
  axisTick: { show: false },
  axisLabel: { color: theme.muted, fontSize: 11 },
  splitLine: { lineStyle: { color: theme.grid, type: "dashed" as const } },
});

export const tooltipStyle = (theme: ChartTheme) => ({
  backgroundColor: theme.surface,
  borderColor: theme.grid,
  textStyle: { color: theme.text, fontSize: 12 },
  extraCssText: "box-shadow: 0 4px 16px rgba(0,0,0,.12); border-radius: 8px;",
});
