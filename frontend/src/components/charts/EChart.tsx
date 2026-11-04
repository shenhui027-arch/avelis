// ECharts 画布（按需引入、单独分块懒加载）：主题从设计令牌读，深浅切换时重画；导出 PNG 用同一实例。
import { BarChart, LineChart, PieChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, MarkLineComponent, MarkPointComponent, TitleComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";

import { chartHeight, chartOption, type ChartSpec, type ChartTheme } from "./chartOption";
import type { NumberStyle } from "../../numbers";

// 标注（极值点、平均线）来自 chart 工具的 marks 参数，按需引入也要注册这两个组件
echarts.use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, DataZoomComponent, TitleComponent,
  MarkPointComponent, MarkLineComponent, CanvasRenderer]);

function readTheme(): ChartTheme {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return {
    series: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => v(`--viz-${i}`)), neutral: v("--viz-neutral"), good: v("--ok"), bad: v("--danger"),
    grid: v("--viz-grid"), axis: v("--viz-axis"), text: v("--text-secondary"), muted: v("--text-tertiary"),
    surface: v("--bg-overlay"), font: v("--font-sans"), mono: v("--font-mono"),
  };
}

/** 主题跟随 <html data-theme> 变化。 */
function useTheme(): ChartTheme {
  const [theme, setTheme] = useState(readTheme);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(readTheme()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

export type EChartHandle = { png: () => string | null };

const EChart = forwardRef<EChartHandle, { spec: ChartSpec; style: NumberStyle; label: string }>(function EChart({ spec, style, label }, ref) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const theme = useTheme();

  useEffect(() => {
    if (!host.current) return;
    const instance = echarts.init(host.current, undefined, { renderer: "canvas" });
    chart.current = instance;
    const resize = new ResizeObserver(() => instance.resize());
    resize.observe(host.current);
    return () => { resize.disconnect(); instance.dispose(); chart.current = null; };
  }, []);

  useEffect(() => {
    chart.current?.setOption(chartOption(spec, style, theme), { notMerge: true });
  }, [spec, style, theme]);

  useImperativeHandle(ref, () => ({
    png: () => chart.current?.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: getComputedStyle(document.documentElement).getPropertyValue("--bg-base").trim() }) ?? null,
  }), []);

  return <div ref={host} className="av-echart" role="img" aria-label={label} style={{ height: chartHeight(spec) }} />;
});

export default EChart;
