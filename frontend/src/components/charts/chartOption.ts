// 图表规格（后端 charts.recommend 产出，已排序、截断、合并“其他”）→ ECharts 配置。纯函数：不读 DOM，便于单测。
// 视觉规则：细线 2px、柱宽 ≤ 24px 且数据端 4px 圆角、网格线退后、文字用文字色而不用系列色、≥ 2 个系列才有图例。
import { formatAxis, formatValue, type NumberStyle, type UnitMeta } from "../../numbers";

export type ChartKind = "kpi" | "line" | "bar" | "grouped_bar" | "stacked_bar" | "diverging_bar" | "donut" | "multiples";
export type ChartSeries = { name: string; column: string; label: string; axis: number; mark: "bar" | "line";
  unit: UnitMeta; polarity: "up_good" | "down_good" | null; values: (number | null)[]; other?: boolean };
type Polarity = ChartSeries["polarity"];
export type ChartKpi = { label: string; column: string; unit: UnitMeta; polarity: "up_good" | "down_good" | null;
  value: number | null; previous: number | null; change: number | null; rate: number | null; previous_label: string | null };
export type ChartSpec = { kind: ChartKind; orientation: "vertical" | "horizontal"; reason: string; title?: string | null;
  x: { column: string; label: string; role: "time" | "dimension"; grain: string | null } | null;
  categories: (string | number | null)[]; series: ChartSeries[]; kpis: ChartKpi[]; others: { count: number } | null; dropped_series: number };

/** 主题取值：由组件从 CSS 令牌读出后传入。 */
export type ChartTheme = { series: string[]; neutral: string; good: string; bad: string; grid: string; axis: string;
  text: string; muted: string; surface: string; font: string; mono: string };

type Option = Record<string, unknown>;

const BAR = 24;

/** 折线只在期间不多、或有前后都为空的孤立值时显示数据点；孤立值不画点就既没有点也没有线。 */
function showMarks(values: (number | null)[], periods: number): boolean {
  return periods <= 12 || values.some((v, i) => v != null && values[i - 1] == null && values[i + 1] == null);
}

/** 系列颜色按固定顺序分配，“其他”用中性色；颜色跟着系列走，不按排名。 */
export function seriesColor(spec: ChartSpec, index: number, theme: ChartTheme): string {
  return spec.series[index]?.other ? theme.neutral : theme.series[index % theme.series.length];
}

/** 变化的好坏只看期望方向：越高越好时上升为好；方向未知或没有变化时不着色。 */
export function changeColorTone(value: number | null, polarity: Polarity): "ok" | "danger" | "" {
  if (value == null || value === 0 || !polarity) return "";
  return (value > 0) === (polarity === "up_good") ? "ok" : "danger";
}

/** 变化条的颜色：好为 ok、坏为 danger，未知为中性。 */
export function changeColor(value: number | null, polarity: Polarity, theme: ChartTheme): string {
  const tone = changeColorTone(value, polarity);
  return tone === "ok" ? theme.good : tone === "danger" ? theme.bad : theme.neutral;
}

/** 推荐高度：横向条按类目数，多图按面板数，其余固定。 */
export function chartHeight(spec: ChartSpec): number {
  if (spec.kind === "multiples") return spec.series.length * 150 + 24;
  if (spec.orientation === "horizontal") return Math.max(160, spec.categories.length * (spec.series.length > 1 ? 40 : 28) + 56);
  if (spec.kind === "donut") return 260;
  return 280;
}

function axisLabel(theme: ChartTheme) {
  return { color: theme.muted, fontFamily: theme.font, fontSize: 11, hideOverlap: true };
}

function valueAxis(unit: UnitMeta, style: NumberStyle, theme: ChartTheme, extra: Option = {}): Option {
  return { type: "value", splitNumber: 4, axisLabel: { ...axisLabel(theme), formatter: (v: number) => formatAxis(v, unit, style) },
    splitLine: { lineStyle: { color: theme.grid, width: 1 } }, axisLine: { show: false }, axisTick: { show: false }, ...extra };
}

function categoryAxis(spec: ChartSpec, theme: ChartTheme, extra: Option = {}): Option {
  return { type: "category", data: spec.categories.map((c) => (c == null ? "—" : String(c))), axisLabel: { ...axisLabel(theme), width: 120, overflow: "truncate" },
    axisLine: { lineStyle: { color: theme.axis } }, axisTick: { show: false }, ...extra };
}

function tooltip(spec: ChartSpec, style: NumberStyle, theme: ChartTheme, trigger: "axis" | "item"): Option {
  return {
    trigger, confine: true, backgroundColor: theme.surface, borderColor: theme.grid, padding: [8, 10],
    textStyle: { color: theme.text, fontFamily: theme.font, fontSize: 12 },
    axisPointer: { type: trigger === "axis" && spec.kind === "line" ? "line" : "shadow", lineStyle: { color: theme.axis } },
    formatter: (params: unknown) => {
      const items = (Array.isArray(params) ? params : [params]) as { seriesIndex: number; dataIndex: number; marker: string; name: string; seriesName: string; value: unknown; percent?: number }[];
      if (!items.length) return "";
      const head = escape(items[0].name);
      const lines = items.map((p) => {
        const s = spec.series[p.seriesIndex] ?? spec.series[0];
        const raw = s?.values[p.dataIndex] ?? null;
        const pct = p.percent != null ? ` · ${p.percent.toFixed(1)}%` : "";
        return `${p.marker}${escape(spec.series.length > 1 || spec.kind === "multiples" ? p.seriesName : s?.label ?? "")}` +
          `<span style="float:right;margin-left:16px;font-family:${theme.mono}">${escape(formatValue(raw, s?.unit, style))}${pct}</span>`;
      });
      return `<div style="font-weight:500;margin-bottom:4px">${head}</div>${lines.join("<br/>")}`;
    },
  };
}

function legend(spec: ChartSpec, theme: ChartTheme): Option | undefined {
  if (spec.series.length < 2 || spec.kind === "multiples" || spec.kind === "donut") return undefined;
  return { type: "scroll", top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 16,
    textStyle: { color: theme.text, fontFamily: theme.font, fontSize: 12 }, pageTextStyle: { color: theme.muted } };
}

function escape(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/** 规格 → ECharts 配置；kpi 由组件直接用 HTML 渲染，不走这里。 */
export function chartOption(spec: ChartSpec, style: NumberStyle, theme: ChartTheme): Option {
  const base: Option = { animation: true, animationDuration: 300, textStyle: { fontFamily: theme.font }, backgroundColor: "transparent" };
  const top = spec.series.length > 1 && spec.kind !== "multiples" && spec.kind !== "donut" ? 36 : 12;
  const unit = spec.series[0]?.unit ?? {};
  const horizontal = spec.orientation === "horizontal";
  const radius = horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0];
  const many = spec.categories.length > 60;

  if (spec.kind === "donut") {
    const s = spec.series[0];
    return { ...base, tooltip: tooltip(spec, style, theme, "item"),
      series: [{ type: "pie", radius: ["52%", "76%"], center: ["50%", "50%"], padAngle: 1, itemStyle: { borderRadius: 4 },
        label: { color: theme.text, fontFamily: theme.font, fontSize: 12, formatter: "{b}  {d}%" },
        labelLine: { lineStyle: { color: theme.axis } },
        data: spec.categories.map((name, i) => ({ name: name == null ? "—" : String(name), value: s.values[i],
          itemStyle: { color: spec.others && i === spec.categories.length - 1 ? theme.neutral : theme.series[i % theme.series.length] } })) }] };
  }

  if (spec.kind === "multiples") {
    const n = spec.series.length;
    const height = 150, gap = 24;
    return { ...base, tooltip: tooltip(spec, style, theme, "axis"), axisPointer: { link: [{ xAxisIndex: "all" }] },
      title: spec.series.map((s, i) => ({ text: s.name, top: i * (height) , left: 0,
        textStyle: { color: theme.text, fontFamily: theme.font, fontSize: 12, fontWeight: 500 } })),
      grid: spec.series.map((_, i) => ({ left: 8, right: 16, top: i * height + gap, height: height - gap - 22, containLabel: true })),
      xAxis: spec.series.map((_, i) => categoryAxis(spec, theme, { gridIndex: i, axisLabel: { ...axisLabel(theme), show: i === n - 1 } })),
      yAxis: spec.series.map((s, i) => valueAxis(s.unit, style, theme, { gridIndex: i, splitNumber: 2 })),
      series: spec.series.map((s, i) => ({ type: s.mark, name: s.name, xAxisIndex: i, yAxisIndex: i, data: s.values,
        color: seriesColor(spec, i, theme), showSymbol: s.mark === "line" && showMarks(s.values, spec.categories.length),
        symbolSize: 6, lineStyle: { width: 2 }, barMaxWidth: BAR,
        itemStyle: s.mark === "bar" ? { borderRadius: [4, 4, 0, 0] } : undefined })) };
  }

  const negative = spec.series.some((s) => s.values.some((v) => v != null && v < 0));
  const grid = { left: horizontal && negative ? 64 : 8, right: horizontal ? 64 : 16, top, bottom: many ? 48 : 8, containLabel: true };
  const cats = categoryAxis(spec, theme, horizontal ? { inverse: true } : { boundaryGap: spec.kind !== "line" });
  const vals = valueAxis(unit, style, theme);
  const dataZoom = many && !horizontal ? [{ type: "inside" }, { type: "slider", height: 18, bottom: 8, borderColor: theme.grid,
    textStyle: { color: theme.muted }, fillerColor: "rgba(127,127,127,0.12)", handleSize: 14 }] : undefined;
  const labelled = spec.series.length === 1 && spec.categories.length <= 20 && spec.kind !== "line";

  const series = spec.series.map((s, i) => {
    const color = seriesColor(spec, i, theme);
    if (spec.kind === "line") {
      const marks = showMarks(s.values, spec.categories.length);
      return { type: "line", name: s.name, data: s.values, color, connectNulls: false, showSymbol: marks, symbolSize: 8,
        lineStyle: { width: 2 }, itemStyle: { borderColor: theme.surface, borderWidth: 2 },
        areaStyle: spec.series.length === 1 ? { opacity: 0.1 } : undefined, emphasis: { focus: "series" } };
    }
    const diverging = spec.kind === "diverging_bar";
    return { type: "bar", name: s.name, barMaxWidth: BAR, stack: spec.kind === "stacked_bar" ? "total" : undefined,
      color, emphasis: { focus: "series" },
      // 数值标注放在条形末端外侧：负值在左（或下），正值在右（或上）
      data: s.values.map((v) => ({ value: v, label: labelled ? { position: v != null && v < 0 ? (horizontal ? "left" : "bottom") : (horizontal ? "right" : "top") } : undefined, itemStyle: {
        color: diverging ? changeColor(v, s.polarity, theme) : color,
        borderRadius: diverging && v != null && v < 0 ? (horizontal ? [4, 0, 0, 4] : [0, 0, 4, 4]) : spec.kind === "stacked_bar" ? 0 : radius,
        borderColor: spec.kind === "stacked_bar" ? theme.surface : undefined, borderWidth: spec.kind === "stacked_bar" ? 1 : 0 } })),
      label: labelled ? { show: true, position: horizontal ? "right" : "top", color: theme.muted, fontFamily: theme.mono, fontSize: 11,
        formatter: (p: { dataIndex: number }) => formatValue(s.values[p.dataIndex], s.unit, style) } : undefined };
  });

  return { ...base, grid, legend: legend(spec, theme), tooltip: tooltip(spec, style, theme, "axis"), dataZoom,
    xAxis: horizontal ? vals : cats, yAxis: horizontal ? cats : vals, series };
}
