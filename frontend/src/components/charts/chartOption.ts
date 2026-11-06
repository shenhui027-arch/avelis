// 图表规格（后端 charts.recommend 产出，已排序、截断、合并“其他”）→ ECharts 配置。纯函数：不读 DOM，便于单测。
// 视觉规则：细线 2px、柱宽 ≤ 24px 且数据端 4px 圆角、网格线退后、文字用文字色而不用系列色、≥ 2 个系列才有图例。
import { t } from "../../i18n";
import { formatAxis, formatValue, type NumberStyle, type UnitMeta } from "../../numbers";

export type ChartKind = "kpi" | "line" | "area" | "bar" | "grouped_bar" | "stacked_bar" | "diverging_bar" | "donut" | "multiples"
  | "combo" | "pareto" | "waterfall" | "heatmap" | "scatter";
export type ChartSeries = { name: string; column: string; label: string; axis: number; mark: "bar" | "line";
  unit: UnitMeta; polarity: "up_good" | "down_good" | null; values: (number | null)[]; other?: boolean };
type Polarity = ChartSeries["polarity"];
export type ChartKpi = { label: string; column: string; unit: UnitMeta; polarity: "up_good" | "down_good" | null;
  value: number | null; previous: number | null; change: number | null; rate: number | null; previous_label: string | null };
export type ChartSpec = { kind: ChartKind; orientation: "vertical" | "horizontal"; reason: string; title?: string | null;
  x: { column: string; label: string; role: "time" | "dimension"; grain: string | null } | null;
  categories: (string | number | null)[]; series: ChartSeries[]; kpis: ChartKpi[]; others: { count: number } | null; dropped_series: number;
  /** 未画出、也没合并进“其他”的类目：display_limit 为展示上限，top_n 为显式的前 N 设置。 */
  omitted_categories?: { count: number; reason: "display_limit" | "top_n" } | null;
  /** 模型经 chart 工具要求的标注：极值点与平均线，后端已按所示数据算好（series_index 指向 series 的下标）。 */
  marks?: ChartMark[] | null;
  /** 要突出的类目（完整显示值）；其余类目弱化。 */
  highlight?: string[] | null;
  /** 帕累托：与类目对齐的累计占比（0–1）。 */
  cumulative?: (number | null)[] | null;
  /** 瀑布：起点（上期合计）与终点（本期合计）；中间各柱为 series[0] 的变化量。 */
  waterfall?: { start: { label: string; value: number | null }; end: { label: string; value: number | null } } | null;
  /** 热力：categories 为横轴（时间），y_categories 为纵轴成员，cells 为 [横下标, 纵下标, 值]。 */
  heatmap?: { y: { column: string; label: string }; y_categories: (string | number | null)[];
    cells: [number, number, number | null][]; min: number | null; max: number | null } | null;
  /** 散点：series 依次为横轴、纵轴与（可选）气泡大小的度量。 */
  points?: { name: string; x: number | null; y: number | null; size: number | null }[] | null };
export type ChartMark = { type: "max" | "min" | "average"; series: string; series_index: number;
  x: string | number | null; value: number | null };

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
  if (spec.kind === "heatmap") return Math.max(200, (spec.heatmap?.y_categories.length ?? 4) * 28 + 96);
  if (spec.kind === "scatter") return 320;
  return 280;
}

/** #rrggbb / #rgb → rgba；其他写法（如 rgb()）原样返回，交给调用方用整体透明度兜底。 */
export function fade(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return color;
  const hex = m[1].length === 3 ? m[1].split("").map((c) => c + c).join("") : m[1];
  const n = parseInt(hex, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/** 面积的纵向渐变：线下由浓到无，比单色淡填充更有层次。 */
function areaFill(color: string, strength: number): Option {
  if (fade(color, 0) === color) return { opacity: strength * 0.5 };
  return { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [{ offset: 0, color: fade(color, strength) }, { offset: 1, color: fade(color, 0) }] } };
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
    axisPointer: { type: trigger === "axis" && (spec.kind === "line" || spec.kind === "area") ? "line" : "shadow", lineStyle: { color: theme.axis } },
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

const DIM = 0.32;

/** 前后都为空的孤立观测：折线画不出线段，只剩数据点，弱化时也不能把点藏掉。 */
function isolated(values: (number | null)[], j: number): boolean {
  return values[j] != null && values[j - 1] == null && values[j + 1] == null;
}

/** 折线上突出某些期间：只在这些点画大号数据点，其余点隐藏；孤立观测保留弱化的小点。 */
function linePoints(spec: ChartSpec, values: (number | null)[]) {
  return values.map((v, j) => !dimmed(spec, j) ? { value: v, symbolSize: 10 }
    : isolated(values, j) ? { value: v, symbolSize: 6, itemStyle: { opacity: DIM } } : { value: v, symbolSize: 0 });
}

function category(c: string | number | null): string {
  return c == null ? "—" : String(c);
}

/** 突出类目时其余类目弱化；没有要求时全部正常。 */
function dimmed(spec: ChartSpec, index: number): boolean {
  return !!spec.highlight?.length && !spec.highlight.includes(category(spec.categories[index]));
}

/** 极值点与平均线：只画规格里给出的标注，位置与数值都来自后端，不在这里计算。 */
function markOptions(spec: ChartSpec, index: number, style: NumberStyle, theme: ChartTheme, horizontal: boolean): Option {
  const s = spec.series[index];
  const marks = (spec.marks ?? []).filter((m) => m.series_index === index && m.value != null);
  if (!s || !marks.length) return {};
  const label = (m: ChartMark) => `${m.type === "max" ? t("最高") : m.type === "min" ? t("最低") : t("平均")} ${formatValue(m.value, s.unit, style)}`;
  const text = { color: theme.text, fontFamily: theme.font, fontSize: 11 };
  const stacked = spec.kind === "stacked_bar";
  // 堆叠柱里标注要指向本段的末端：位置 = 同方向前序各段之和 + 本段值（正负分开堆叠，与 ECharts 一致）；标签仍是本段自身的值
  const end = (m: ChartMark) => {
    const v = m.value as number;
    if (!stacked) return v;
    const j = spec.categories.findIndex((c) => category(c) === category(m.x));
    return spec.series.slice(0, index).reduce((sum, other) => {
      const w = j >= 0 ? other.values[j] : null;
      return w != null && (w < 0) === (v < 0) ? sum + w : sum;
    }, v);
  };
  const points = marks.filter((m) => m.type !== "average").map((m) => {
    const at = category(m.x);
    return { name: label(m), coord: horizontal ? [end(m), at] : [at, end(m)], value: m.value,
      label: { ...text, formatter: label(m), position: horizontal ? "right" : "top", distance: 10 } };
  });
  // 堆叠图上平均线画不到本段的位置，改在图下用文字给出（ChartPanel）
  const lines = marks.filter((m) => m.type === "average" && !stacked).map((m) => ({ name: label(m), ...(horizontal ? { xAxis: m.value } : { yAxis: m.value }),
    label: { ...text, formatter: label(m), position: horizontal ? "end" : "insideEndTop" } }));
  return {
    markPoint: points.length ? { symbol: "circle", symbolSize: 9, itemStyle: { color: theme.surface, borderColor: theme.text, borderWidth: 2 },
      data: points, silent: true } : undefined,
    markLine: lines.length ? { symbol: "none", silent: true, lineStyle: { color: theme.muted, type: "dashed", width: 1 }, data: lines } : undefined,
  };
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
          itemStyle: { color: spec.others && i === spec.categories.length - 1 ? theme.neutral : theme.series[i % theme.series.length],
            opacity: dimmed(spec, i) ? DIM : 1 } })) }] };
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
      series: spec.series.map((s, i) => ({ type: s.mark, name: s.name, xAxisIndex: i, yAxisIndex: i,
        // 折线面板的点平时可能不显示（期间多时），突出期间时要把那些点画出来
        data: s.mark === "line" && spec.highlight?.length ? linePoints(spec, s.values)
          : s.values.map((v, j) => ({ value: v, itemStyle: { opacity: dimmed(spec, j) ? DIM : 1 } })),
        color: seriesColor(spec, i, theme),
        showSymbol: s.mark === "line" && (showMarks(s.values, spec.categories.length) || !!spec.highlight?.length),
        symbolSize: 6, lineStyle: { width: 2 }, barMaxWidth: BAR,
        itemStyle: s.mark === "bar" ? { borderRadius: [4, 4, 0, 0] } : undefined, ...markOptions(spec, i, style, theme, false) })) };
  }

  if (spec.kind === "heatmap" && spec.heatmap) return heatmapOption(spec, spec.heatmap, base, style, theme);
  if (spec.kind === "scatter" && spec.points) return scatterOption(spec, spec.points, base, style, theme);
  if (spec.kind === "waterfall" && spec.waterfall) return waterfallOption(spec, spec.waterfall, base, style, theme);
  if (spec.kind === "pareto" && spec.cumulative) return paretoOption(spec, spec.cumulative, base, style, theme);
  if (spec.kind === "combo" && spec.series.length >= 2) return comboOption(spec, base, style, theme);

  const negative = spec.series.some((s) => s.values.some((v) => v != null && v < 0));
  const grid = { left: 8, right: horizontal ? 64 : 16, top, bottom: many ? 48 : 8, containLabel: true };
  const lineLike = spec.kind === "line" || spec.kind === "area";
  const cats = categoryAxis(spec, theme, horizontal ? { inverse: true } : { boundaryGap: !lineLike });
  const labelled = spec.series.length === 1 && spec.categories.length <= 20 && !lineLike;
  // 负值的数值标注在条形外侧：数值轴向负方向留出余量，最长的负值条不顶到边，标注不压住类目轴文字
  const vals = valueAxis(unit, style, theme, labelled && negative ? { boundaryGap: ["18%", 0] } : {});
  const dataZoom = many && !horizontal ? [{ type: "inside" }, { type: "slider", height: 18, bottom: 8, borderColor: theme.grid,
    textStyle: { color: theme.muted }, fillerColor: "rgba(127,127,127,0.12)", handleSize: 14 }] : undefined;

  const series = spec.series.map((s, i) => {
    const color = seriesColor(spec, i, theme);
    if (spec.kind === "line" || spec.kind === "area") {
      const marks = showMarks(s.values, spec.categories.length);
      const data = spec.highlight?.length ? linePoints(spec, s.values) : s.values;
      // 单条折线与面积图带纵向渐变；面积图多系列时每条都淡一些，互相不遮挡
      const fill = spec.kind === "area" ? areaFill(color, spec.series.length > 1 ? 0.22 : 0.38)
        : spec.series.length === 1 ? areaFill(color, 0.24) : undefined;
      return { type: "line", name: s.name, data, color, connectNulls: false, showSymbol: marks || !!spec.highlight?.length, symbolSize: 8,
        lineStyle: { width: spec.kind === "area" ? 2.5 : 2 }, itemStyle: { borderColor: theme.surface, borderWidth: 2 },
        areaStyle: fill, emphasis: { focus: "series" },
        ...markOptions(spec, i, style, theme, false) };
    }
    const diverging = spec.kind === "diverging_bar";
    return { type: "bar", name: s.name, barMaxWidth: BAR, stack: spec.kind === "stacked_bar" ? "total" : undefined,
      color, emphasis: { focus: "series" },
      // 数值标注放在条形末端外侧：负值在左（或下），正值在右（或上）
      data: s.values.map((v, j) => ({ value: v, label: labelled ? { position: v != null && v < 0 ? (horizontal ? "left" : "bottom") : (horizontal ? "right" : "top") } : undefined, itemStyle: {
        color: diverging ? changeColor(v, s.polarity, theme) : color, opacity: dimmed(spec, j) ? DIM : 1,
        borderRadius: diverging && v != null && v < 0 ? (horizontal ? [4, 0, 0, 4] : [0, 0, 4, 4]) : spec.kind === "stacked_bar" ? 0 : radius,
        borderColor: spec.kind === "stacked_bar" ? theme.surface : undefined, borderWidth: spec.kind === "stacked_bar" ? 1 : 0 } })),
      label: labelled ? { show: true, position: horizontal ? "right" : "top", color: theme.muted, fontFamily: theme.mono, fontSize: 11,
        formatter: (p: { dataIndex: number }) => formatValue(s.values[p.dataIndex], s.unit, style) } : undefined,
      ...markOptions(spec, i, style, theme, horizontal) };
  });

  return { ...base, grid, legend: legend(spec, theme), tooltip: tooltip(spec, style, theme, "axis"), dataZoom,
    xAxis: horizontal ? vals : cats, yAxis: horizontal ? cats : vals, series };
}

// ---- 新图种：热力、散点、瀑布、帕累托、柱线组合。数据都来自规格，这里只决定怎么画。 ----

function tipBox(theme: ChartTheme): Option {
  return { confine: true, backgroundColor: theme.surface, borderColor: theme.grid, padding: [8, 10],
    textStyle: { color: theme.text, fontFamily: theme.font, fontSize: 12 } };
}

function tipHead(text: string): string {
  return `<div style="font-weight:500;margin-bottom:4px">${escape(text)}</div>`;
}

function tipRow(label: string, value: string, theme: ChartTheme): string {
  return `${escape(label)}<span style="float:right;margin-left:16px;font-family:${theme.mono}">${escape(value)}</span>`;
}

function signed(value: number | null, unit: UnitMeta, style: NumberStyle): string {
  return value != null && value > 0 ? `+${formatValue(value, unit, style)}` : formatValue(value, unit, style);
}

/** 热力：时间 × 成员，颜色由浅到品牌色，格子之间留缝。 */
function heatmapOption(spec: ChartSpec, h: NonNullable<ChartSpec["heatmap"]>, base: Option, style: NumberStyle, theme: ChartTheme): Option {
  const s = spec.series[0];
  const unit = s?.unit ?? {};
  const color = theme.series[0];
  const xs = spec.categories.map(category);
  const ys = h.y_categories.map(category);
  const low = h.min ?? 0;
  const high = h.max != null && h.max !== low ? h.max : low + 1;
  return { ...base,
    tooltip: { ...tipBox(theme), trigger: "item", formatter: (p: { value: [number, number, number | string] }) => {
      const [i, j, v] = p.value;
      return tipHead(`${ys[j]} · ${xs[i]}`) + tipRow(s?.name ?? "", formatValue(typeof v === "number" ? v : null, unit, style), theme);
    } },
    grid: { left: 8, right: 16, top: 8, bottom: 48, containLabel: true },
    xAxis: { type: "category", data: xs, axisLabel: axisLabel(theme), axisLine: { show: false }, axisTick: { show: false }, splitArea: { show: false } },
    yAxis: { type: "category", data: ys, inverse: true, axisLabel: { ...axisLabel(theme), width: 120, overflow: "truncate" },
      axisLine: { show: false }, axisTick: { show: false } },
    visualMap: { type: "continuous", min: low, max: high, calculable: false, orient: "horizontal", left: "center", bottom: 0,
      itemWidth: 10, itemHeight: 160, text: [formatAxis(high, unit, style), formatAxis(low, unit, style)],
      textStyle: { color: theme.muted, fontFamily: theme.font, fontSize: 11 },
      inRange: { color: [fade(color, 0.08), fade(color, 0.5), color] } },
    series: [{ type: "heatmap", name: s?.name, data: h.cells.map(([i, j, v]) => [i, j, v ?? "-"]),
      itemStyle: { borderColor: theme.surface, borderWidth: 2, borderRadius: 4 },
      emphasis: { itemStyle: { borderColor: theme.text, borderWidth: 1 } } }] };
}

/** 散点 / 气泡：横纵轴各是一个度量，第三个度量决定气泡大小（面积与值成正比）。 */
function scatterOption(spec: ChartSpec, points: NonNullable<ChartSpec["points"]>, base: Option, style: NumberStyle, theme: ChartTheme): Option {
  const [sx, sy, ss] = spec.series;
  const color = theme.series[0];
  const biggest = Math.max(0, ...points.map((p) => Math.abs(p.size ?? 0)));
  const radius = (size: number | null) => ss && biggest && size != null ? 10 + 30 * Math.sqrt(Math.abs(size) / biggest) : 12;
  const name = (text: string | undefined) => ({ name: text ?? "", nameTextStyle: { color: theme.muted, fontFamily: theme.font, fontSize: 11 } });
  const labelled = points.length <= 15;
  return { ...base,
    tooltip: { ...tipBox(theme), trigger: "item", formatter: (p: { dataIndex: number }) => {
      const point = points[p.dataIndex];
      if (!point) return "";
      return tipHead(point.name) + [tipRow(sx?.name ?? "", formatValue(point.x, sx?.unit, style), theme),
        tipRow(sy?.name ?? "", formatValue(point.y, sy?.unit, style), theme),
        ss ? tipRow(ss.name, formatValue(point.size, ss.unit, style), theme) : ""].filter(Boolean).join("<br/>");
    } },
    grid: { left: 8, right: 24, top: 28, bottom: 28, containLabel: true },
    xAxis: valueAxis(sx?.unit ?? {}, style, theme, { scale: true, nameLocation: "middle", nameGap: 26, ...name(sx?.name) }),
    yAxis: valueAxis(sy?.unit ?? {}, style, theme, { scale: true, ...name(sy?.name) }),
    series: [{ type: "scatter", data: points.map((p, i) => ({ name: p.name, value: [p.x, p.y],
      symbolSize: radius(p.size),
      itemStyle: { color: fade(color, 0.55), borderColor: color, borderWidth: 1.5,
        opacity: spec.highlight?.length && !spec.highlight.includes(p.name) ? DIM : 1 },
      label: labelled ? { show: true, position: "right", formatter: p.name, color: theme.muted, fontFamily: theme.font, fontSize: 11 } : undefined,
      z: points.length - i })),
      emphasis: { focus: "self", itemStyle: { color } } }] };
}

/** 瀑布：上期合计 → 各成员的增减（浮动柱）→ 本期合计。累计跨到负值时退回为变化条。 */
function waterfallOption(spec: ChartSpec, w: NonNullable<ChartSpec["waterfall"]>, base: Option, style: NumberStyle, theme: ChartTheme): Option {
  const s = spec.series[0];
  const unit = s?.unit ?? {};
  const deltas = s?.values ?? [];
  const floor: (number | null)[] = [0];
  let run = w.start.value ?? 0;
  for (const d of deltas) {
    floor.push(d == null ? null : d >= 0 ? run : run + d);
    run += d ?? 0;
  }
  floor.push(0);
  if ((w.start.value ?? 0) < 0 || (w.end.value ?? 0) < 0 || floor.some((v) => v != null && v < 0)) {
    return chartOption({ ...spec, kind: "diverging_bar", waterfall: null }, style, theme);
  }
  // 期望方向已知时增减按好坏着色；未知时增为绿、减为红，只表示方向
  const tone = (d: number) => s?.polarity ? changeColor(d, s.polarity, theme) : d >= 0 ? theme.series[1] : theme.series[3];
  const cats = [w.start.label, ...spec.categories.map(category), w.end.label];
  const bars = [
    { value: w.start.value, delta: null as number | null, itemStyle: { color: theme.neutral, opacity: 1 } },
    ...deltas.map((d, j) => ({ value: d == null ? null : Math.abs(d), delta: d,
      itemStyle: { color: d == null || (spec.others && j === deltas.length - 1) ? theme.neutral : tone(d), opacity: dimmed(spec, j) ? DIM : 1 } })),
    { value: w.end.value, delta: null as number | null, itemStyle: { color: theme.series[0], opacity: 1 } },
  ];
  const text = (i: number) => i === 0 || i === bars.length - 1 ? formatValue(bars[i].value, unit, style) : signed(bars[i].delta, unit, style);
  return { ...base,
    tooltip: { ...tipBox(theme), trigger: "axis", axisPointer: { type: "shadow" }, formatter: (params: unknown) => {
      const p = (Array.isArray(params) ? params : [params])[0] as { dataIndex: number } | undefined;
      return p ? tipHead(cats[p.dataIndex]) + tipRow(s?.name ?? "", text(p.dataIndex), theme) : "";
    } },
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    xAxis: categoryAxis({ ...spec, categories: cats }, theme),
    yAxis: valueAxis(unit, style, theme),
    series: [
      { type: "bar", stack: "waterfall", data: floor, itemStyle: { color: "transparent" }, silent: true, tooltip: { show: false },
        emphasis: { disabled: true } },
      { type: "bar", stack: "waterfall", name: s?.name, barMaxWidth: 32, data: bars.map(({ value, itemStyle }) => ({ value, itemStyle })),
        itemStyle: { borderRadius: 4 },
        label: { show: cats.length <= 16, position: "top", color: theme.muted, fontFamily: theme.mono, fontSize: 11,
          formatter: (p: { dataIndex: number }) => text(p.dataIndex) } },
    ] };
}

/** 帕累托：降序柱 + 累计占比折线（右轴 0–100%），80% 处一条参考虚线。 */
function paretoOption(spec: ChartSpec, cumulative: (number | null)[], base: Option, style: NumberStyle, theme: ChartTheme): Option {
  const s = spec.series[0];
  const color = seriesColor(spec, 0, theme);
  const share = t("累计占比");
  return { ...base,
    legend: { top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 16, data: [s?.name ?? "", share],
      textStyle: { color: theme.text, fontFamily: theme.font, fontSize: 12 } },
    tooltip: { ...tipBox(theme), trigger: "axis", axisPointer: { type: "shadow" }, formatter: (params: unknown) => {
      const p = (Array.isArray(params) ? params : [params])[0] as { dataIndex: number; name: string } | undefined;
      if (!p) return "";
      const c = cumulative[p.dataIndex];
      return tipHead(p.name) + tipRow(s?.name ?? "", formatValue(s?.values[p.dataIndex] ?? null, s?.unit, style), theme) + "<br/>" +
        tipRow(share, c == null ? "—" : `${(c * 100).toFixed(1)}%`, theme);
    } },
    grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
    xAxis: categoryAxis(spec, theme),
    yAxis: [valueAxis(s?.unit ?? {}, style, theme),
      { type: "value", min: 0, max: 1, splitNumber: 4, splitLine: { show: false }, axisLine: { show: false }, axisTick: { show: false },
        axisLabel: { ...axisLabel(theme), formatter: (v: number) => `${Math.round(v * 100)}%` } }],
    series: [
      { type: "bar", name: s?.name, barMaxWidth: BAR, color, data: (s?.values ?? []).map((v, j) => ({ value: v, itemStyle: {
        color: spec.others && j === spec.categories.length - 1 ? theme.neutral : color, opacity: dimmed(spec, j) ? DIM : 1,
        borderRadius: [4, 4, 0, 0] } })) },
      { type: "line", name: share, yAxisIndex: 1, data: cumulative, color: theme.series[2], symbolSize: 6, lineStyle: { width: 2 },
        itemStyle: { borderColor: theme.surface, borderWidth: 2 },
        markLine: { symbol: "none", silent: true, lineStyle: { color: theme.muted, type: "dashed", width: 1 },
          label: { color: theme.muted, fontFamily: theme.font, fontSize: 11, formatter: "80%", position: "insideEndTop" }, data: [{ yAxis: 0.8 }] } },
    ] };
}

/** 柱线组合：mark 决定画法，axis 决定左右轴；两个单位不同的度量放在一张图里对照。 */
function comboOption(spec: ChartSpec, base: Option, style: NumberStyle, theme: ChartTheme): Option {
  const axes = Math.min(2, Math.max(...spec.series.map((s) => s.axis)) + 1);
  const unitOf = (axis: number) => spec.series.find((s) => s.axis === axis)?.unit ?? {};
  const yAxis = Array.from({ length: axes }, (_, i) => valueAxis(unitOf(i), style, theme, i ? { splitLine: { show: false } } : {}));
  return { ...base, grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
    legend: legend(spec, theme), tooltip: tooltip(spec, style, theme, "axis"),
    xAxis: categoryAxis(spec, theme), yAxis,
    series: spec.series.map((s, i) => {
      const color = seriesColor(spec, i, theme);
      const axis = Math.min(s.axis, axes - 1);
      return s.mark === "line"
        ? { type: "line", name: s.name, yAxisIndex: axis, data: s.values, color, symbolSize: 7, lineStyle: { width: 2.5 },
          itemStyle: { borderColor: theme.surface, borderWidth: 2 }, z: 3, ...markOptions(spec, i, style, theme, false) }
        : { type: "bar", name: s.name, yAxisIndex: axis, barMaxWidth: BAR, color,
          data: s.values.map((v, j) => ({ value: v, itemStyle: { color, opacity: dimmed(spec, j) ? DIM : 1, borderRadius: [4, 4, 0, 0] } })),
          ...markOptions(spec, i, style, theme, false) };
    }) };
}
