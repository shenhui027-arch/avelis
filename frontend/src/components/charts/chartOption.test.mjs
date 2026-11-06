// Run: node src/components/charts/chartOption.test.mjs (from frontend/).
// 规格 → ECharts 配置：颜色按固定顺序、“其他”中性、变化按期望方向着色、长序列可缩放、横向条形第一名在上、单系列不出图例。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const url = (path, replace = (s) => s) => {
  const source = replace(readFileSync(new URL(path, import.meta.url), "utf8"));
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
  return "data:text/javascript;base64," + Buffer.from(js).toString("base64");
};
const numbers = url("../../numbers.ts", (s) => s.replace(/^import \{ t \} from "\.\/i18n";$/m, "const t = (s) => s;"));
const mod = await import(url("./chartOption.ts", (s) => s.replace('from "../../numbers"', `from "${numbers}"`)
  .replace(/^import \{ t \} from "\.\.\/\.\.\/i18n";$/m, "const t = (s) => s;")));

const theme = { series: ["#s1", "#s2", "#s3", "#s4", "#s5", "#s6", "#s7", "#s8"], neutral: "#n", good: "#g", bad: "#b",
  grid: "#grid", axis: "#axis", text: "#t", muted: "#m", surface: "#bg", font: "sans", mono: "mono" };
const unit = { kind: "level", unit: "元" };
const series = (name, values, extra = {}) => ({ name, column: "value", label: "指标", axis: 0, mark: "bar", unit, polarity: null, values, ...extra });
const spec = (extra) => ({ kind: "bar", orientation: "horizontal", reason: "ranking", title: null, x: { column: "d", label: "维度", role: "dimension", grain: null },
  categories: ["甲", "乙", "其他"], series: [series("指标", [3, 2, 1])], kpis: [], others: null, dropped_series: 0, ...extra });

// 横向条形：类目轴倒序（第一名在上），单系列不出图例，带数值标注（按量纲格式化）
let option = mod.chartOption(spec(), "cn", theme);
assert.equal(option.yAxis.type, "category");
assert.equal(option.yAxis.inverse, true);
assert.equal(option.legend, undefined);
assert.equal(option.series[0].label.formatter({ dataIndex: 0 }), "3元");
assert.equal(option.series[0].barMaxWidth, 24);

// 多系列：颜色按固定顺序；“其他”系列用中性色
option = mod.chartOption(spec({ kind: "line", orientation: "vertical", series: [series("甲", [1, 2]), series("乙", [2, 3]), series("其他", [1, 1], { other: true })], categories: ["01", "02"] }), "cn", theme);
assert.deepEqual(option.series.map((s) => s.color), ["#s1", "#s2", "#n"]);
assert.ok(option.legend);
assert.equal(option.dataZoom, undefined);

// 超过 60 个期间开启缩放
const days = Array.from({ length: 90 }, (_, i) => `d${i}`);
option = mod.chartOption(spec({ kind: "line", orientation: "vertical", categories: days, series: [series("指标", days.map((_, i) => i))] }), "cn", theme);
assert.ok(Array.isArray(option.dataZoom));

// 变化条：期望方向已知时好坏着色，未知时中性；负值的圆角在左
const change = (polarity) => mod.chartOption(spec({ kind: "diverging_bar", categories: ["甲", "乙"], series: [series("变化", [5, -3], { polarity, unit: { kind: "delta", unit: "元" } })] }), "cn", theme);
assert.deepEqual(change("up_good").series[0].data.map((d) => d.itemStyle.color), ["#g", "#b"]);
assert.deepEqual(change("down_good").series[0].data.map((d) => d.itemStyle.color), ["#b", "#g"]);
assert.deepEqual(change(null).series[0].data.map((d) => d.itemStyle.color), ["#n", "#n"]);
assert.deepEqual(change("up_good").series[0].data[1].itemStyle.borderRadius, [4, 0, 0, 4]);
assert.equal(mod.changeColorTone(0, "up_good"), "");

// 环形：“其他”是最后一个类目，用中性色
option = mod.chartOption(spec({ kind: "donut", others: { count: 3 } }), "cn", theme);
assert.deepEqual(option.series[0].data.map((d) => d.itemStyle.color), ["#s1", "#s2", "#n"]);

// 分图：每个度量一个面板，各自的纵轴
option = mod.chartOption(spec({ kind: "multiples", orientation: "vertical", categories: ["01", "02"],
  series: [series("金额", [1, 2], { axis: 0 }), series("比率", [0.1, 0.2], { axis: 1, unit: { kind: "ratio" } })] }), "cn", theme);
assert.equal(option.grid.length, 2);
assert.equal(option.yAxis[1].axisLabel.formatter(0.1), "10%");
assert.equal(mod.chartHeight(spec({ kind: "multiples", series: [series("a", []), series("b", [])] })), 324);

// 提示框：按量纲显示，名称转义
const tip = mod.chartOption(spec({ categories: ["<b>"], series: [series("指标", [12345])] }), "cn", theme).tooltip.formatter([{ seriesIndex: 0, dataIndex: 0, marker: "", name: "<b>", seriesName: "指标", value: 12345 }]);
assert.ok(tip.includes("&lt;b&gt;") && tip.includes("1.23万元"));
console.log("chartOption: ok");

// 孤立观测（前后都为空）必须显示数据点，否则既无点也无线：分图与普通折线、长序列都适用
const sparse = (kind, n) => spec({ kind, orientation: "vertical", categories: Array.from({ length: n }, (_, i) => `m${i}`),
  series: [series("北", Array.from({ length: n }, (_, i) => i + 1), { mark: "line" }),
    series("南", Array.from({ length: n }, (_, i) => (i === 0 ? 2 : null)), { mark: "line", axis: kind === "multiples" ? 1 : 0 })] });
option = mod.chartOption(sparse("multiples", 2), "cn", theme);
assert.deepEqual(option.series.map((s) => s.showSymbol), [true, true]);
option = mod.chartOption(sparse("multiples", 30), "cn", theme);
assert.deepEqual(option.series.map((s) => s.showSymbol), [false, true]);
option = mod.chartOption(sparse("line", 30), "cn", theme);
assert.deepEqual(option.series.map((s) => s.showSymbol), [false, true]);
// 分图里的柱形面板不需要数据点
option = mod.chartOption(spec({ kind: "multiples", orientation: "vertical", categories: ["a", "b"], series: [series("甲", [1, null], { mark: "bar" })] }), "cn", theme);
assert.equal(option.series[0].showSymbol, false);
console.log("chartOption isolated-point tests ok");

// 标注：极值点与平均线只画规格里给出的（位置与数值来自后端），横向条形的坐标是 [数值, 类目]
option = mod.chartOption(spec({ marks: [
  { type: "max", series: "value", series_index: 0, x: "甲", value: 3 },
  { type: "average", series: "value", series_index: 0, x: null, value: 2 },
] }), "cn", theme);
assert.deepEqual(option.series[0].markPoint.data.map((d) => d.coord), [[3, "甲"]]);
assert.equal(option.series[0].markPoint.data[0].label.formatter, "最高 3元");
assert.equal(option.series[0].markLine.data[0].xAxis, 2);
assert.equal(option.series[0].markLine.data[0].label.formatter, "平均 2元");
assert.equal(mod.chartOption(spec(), "cn", theme).series[0].markPoint, undefined);

// 突出类目：其余类目弱化，被突出的保持不透明
option = mod.chartOption(spec({ highlight: ["乙"] }), "cn", theme);
assert.deepEqual(option.series[0].data.map((d) => d.itemStyle.opacity), [0.32, 1, 0.32]);
option = mod.chartOption(spec({ kind: "donut", highlight: ["甲"] }), "cn", theme);
assert.deepEqual(option.series[0].data.map((d) => d.itemStyle.opacity), [1, 0.32, 0.32]);
console.log("chartOption marks / highlight: ok");

// 堆叠柱：第二个及之后系列的极值点指向本段末端（加上同方向前序段），标签仍是本段自身的值；正负分开堆叠；平均线不画在堆叠图上
const stackedSpec = (marks) => spec({ kind: "stacked_bar", orientation: "vertical", categories: ["1月", "2月"],
  series: [series("A", [100, -10]), series("B", [20, -30])], marks });
option = mod.chartOption(stackedSpec([
  { type: "max", series: "value", series_index: 1, x: "2月", value: -30 },
  { type: "min", series: "value", series_index: 1, x: "1月", value: 20 },
  { type: "average", series: "value", series_index: 1, x: null, value: -5 },
]), "cn", theme);
assert.deepEqual(option.series[1].markPoint.data.map((d) => d.coord), [["2月", -40], ["1月", 120]]);
assert.equal(option.series[1].markPoint.data[1].label.formatter, "最低 20元");
assert.equal(option.series[1].markLine, undefined);
// 非堆叠图对照：坐标就是原值
option = mod.chartOption(spec({ kind: "grouped_bar", orientation: "vertical", categories: ["1月"], series: [series("A", [100]), series("B", [20])],
  marks: [{ type: "max", series: "value", series_index: 1, x: "1月", value: 20 }] }), "cn", theme);
assert.deepEqual(option.series[1].markPoint.data[0].coord, ["1月", 20]);
console.log("chartOption stacked marks: ok");

// 折线突出某期：孤立观测（前后为空）保留弱化的小点，不被隐藏；分面板折线在期间多时也画出突出的点
const lineSpec = (extra) => spec({ kind: "line", orientation: "vertical", categories: ["1月", "2月", "3月"],
  series: [series("A", [null, 5, null], { mark: "line" })], ...extra });
option = mod.chartOption(lineSpec({ highlight: ["1月"] }), "cn", theme);
assert.deepEqual(option.series[0].data.map((d) => d.symbolSize), [10, 6, 0]);
assert.equal(option.series[0].data[1].itemStyle.opacity, 0.32);
const periods = Array.from({ length: 20 }, (_, i) => `D${i + 1}`);
option = mod.chartOption(spec({ kind: "multiples", orientation: "vertical", categories: periods, highlight: ["D5"],
  series: [series("A", periods.map((_, i) => i + 1), { mark: "line" })] }), "cn", theme);
assert.equal(option.series[0].showSymbol, true);
assert.equal(option.series[0].data[4].symbolSize, 10);
assert.equal(option.series[0].data[0].symbolSize, 0);
console.log("chartOption line highlight: ok");


// 新图种：面积带渐变、帕累托右轴累计、瀑布浮动柱、热力 visualMap、散点气泡、柱线双轴
{
  const hex = { ...theme, series: ["#1e9df1", "#00b87a", "#f7b928", "#e0245e", "#8b5cf6", "#14b8c4", "#f97316", "#64748b"] };
  assert.equal(mod.fade("#1e9df1", 0.5), "rgba(30, 157, 241, 0.5)");
  let o = mod.chartOption(spec({ kind: "area", orientation: "vertical", categories: ["01", "02", "03"], series: [series("甲", [1, 2, 3])] }), "cn", hex);
  assert.equal(o.series[0].type, "line");
  assert.equal(o.series[0].areaStyle.color.type, "linear");

  o = mod.chartOption(spec({ kind: "pareto", orientation: "vertical", cumulative: [0.5, 0.83, 1] }), "cn", hex);
  assert.equal(o.series.length, 2);
  assert.equal(o.series[1].yAxisIndex, 1);
  assert.equal(o.yAxis[1].max, 1);

  o = mod.chartOption(spec({ kind: "waterfall", orientation: "vertical", series: [series("指标", [5, -2, 1])],
    waterfall: { start: { label: "上期", value: 10 }, end: { label: "本期", value: 14 } } }), "cn", hex);
  assert.deepEqual(o.series[0].data, [0, 10, 13, 13, 0]);
  assert.deepEqual(o.series[1].data.map((d) => d.value), [10, 5, 2, 1, 14]);
  assert.equal(o.xAxis.data.length, 5);
  // 累计跨到负值时退回变化条
  o = mod.chartOption(spec({ kind: "waterfall", orientation: "vertical", series: [series("指标", [-20, 1, 1])],
    waterfall: { start: { label: "上期", value: 10 }, end: { label: "本期", value: -8 } } }), "cn", hex);
  assert.equal(o.series[0].type, "bar");
  assert.equal(o.series.length, 1);

  o = mod.chartOption(spec({ kind: "heatmap", orientation: "vertical", categories: ["01", "02", "03"], series: [series("指标", [])],
    heatmap: { y: { column: "d", label: "维度" }, y_categories: ["甲", "乙"], cells: [[0, 0, 1], [1, 1, null]], min: 1, max: 1 } }), "cn", hex);
  assert.equal(o.series[0].type, "heatmap");
  assert.deepEqual(o.series[0].data[1], [1, 1, "-"]);
  assert.ok(o.visualMap.max > o.visualMap.min);
  assert.ok(mod.chartHeight({ kind: "heatmap", heatmap: { y_categories: Array(10) } }) >= 280);

  o = mod.chartOption(spec({ kind: "scatter", orientation: "vertical", series: [series("x", [1, 2, 3]), series("y", [3, 2, 1]), series("s", [1, 4, 9])],
    points: [{ name: "甲", x: 1, y: 3, size: 1 }, { name: "乙", x: 2, y: 2, size: 4 }, { name: "丙", x: 3, y: 1, size: 9 }] }), "cn", hex);
  assert.equal(o.series[0].type, "scatter");
  assert.ok(o.series[0].data[2].symbolSize > o.series[0].data[0].symbolSize);

  o = mod.chartOption(spec({ kind: "combo", orientation: "vertical", series: [series("额", [1, 2, 3]), series("率", [0.1, 0.2, 0.3], { mark: "line", axis: 1 })] }), "cn", hex);
  assert.equal(o.yAxis.length, 2);
  assert.deepEqual(o.series.map((s) => [s.type, s.yAxisIndex]), [["bar", 0], ["line", 1]]);
  console.log("chartOption new kinds: ok");
}

// 帕累托：chart 工具要求的极值与平均标注画在柱系列上
{
  const o = mod.chartOption(spec({ kind: "pareto", orientation: "vertical", cumulative: [0.5, 0.83, 1],
    marks: [{ type: "max", series: "指标", series_index: 0, x: "甲", value: 3 }, { type: "average", series: "指标", series_index: 0, x: null, value: 2 }] }), "cn", theme);
  assert.equal(o.series[0].markPoint.data.length, 1);
  assert.equal(o.series[0].markLine.data.length, 1);
  console.log("chartOption pareto marks: ok");
}

// 热力：突出期间时其余期间的格子弱化
{
  const o = mod.chartOption(spec({ kind: "heatmap", orientation: "vertical", categories: ["01", "02"], series: [series("指标", [])], highlight: ["02"],
    heatmap: { y: { column: "d", label: "维度" }, y_categories: ["甲"], cells: [[0, 0, 1], [1, 0, 2]], min: 1, max: 2 } }), "cn", theme);
  assert.equal(o.series[0].data[0].itemStyle.opacity < 1, true);
  assert.deepEqual(o.series[0].data[1], [1, 0, 2]);
  console.log("chartOption heatmap highlight: ok");
}
