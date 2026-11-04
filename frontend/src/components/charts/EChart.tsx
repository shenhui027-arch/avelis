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

/** note：图上画不出、只在图下以文字给出的标注；下载 PNG 时一并画进图片底部，图片单独拿出去也不丢信息。 */
export type EChartHandle = { png: (note?: string) => Promise<string | null> };

const RATIO = 2;

/** 把文字说明按宽度折行后画在图片底部。 */
async function withNote(url: string, note: string, background: string, theme: ChartTheme): Promise<string> {
  const image = new Image();
  await new Promise<void>((resolve, reject) => { image.onload = () => resolve(); image.onerror = reject; image.src = url; });
  const size = 12 * RATIO, line = 18 * RATIO, pad = 12 * RATIO;
  const probe = document.createElement("canvas").getContext("2d")!;
  probe.font = `${size}px ${theme.font}`;
  const lines: string[] = [];
  let current = "";
  for (const char of note) {
    if (current && probe.measureText(current + char).width > image.width - pad * 2) { lines.push(current); current = ""; }
    current += char;
  }
  if (current) lines.push(current);
  const canvas = document.createElement("canvas");
  canvas.width = image.width;
  canvas.height = image.height + pad + lines.length * line;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(image, 0, 0);
  ctx.fillStyle = theme.text;
  ctx.font = `${size}px ${theme.font}`;
  ctx.textBaseline = "top";
  lines.forEach((text, i) => ctx.fillText(text, pad, image.height + pad / 2 + i * line));
  return canvas.toDataURL("image/png");
}

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
    png: async (note?: string) => {
      const background = getComputedStyle(document.documentElement).getPropertyValue("--bg-base").trim();
      const url = chart.current?.getDataURL({ type: "png", pixelRatio: RATIO, backgroundColor: background }) ?? null;
      return url && note ? withNote(url, note, background, theme) : url;
    },
  }), [theme]);

  return <div ref={host} className="av-echart" role="img" aria-label={label} style={{ height: chartHeight(spec) }} />;
});

export default EChart;
