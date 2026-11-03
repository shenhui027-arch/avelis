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
const mod = await import(url("./chartOption.ts", (s) => s.replace('from "../../numbers"', `from "${numbers}"`)));

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
