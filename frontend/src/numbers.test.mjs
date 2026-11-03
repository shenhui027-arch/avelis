// Run: node src/numbers.test.mjs (from frontend/).
// 数值显示与后端 grounding.display 一致：量纲决定格式，cn / intl 都从绝对值 1 万起缩写。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./numbers.ts", import.meta.url), "utf8")
  .replace(/^import \{ t \} from "\.\/i18n";$/m, "const t = (s) => s;");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { formatValue, formatAxis, unitLabel, toNumber } = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

const yuan = { kind: "level", unit: "元" };
// 水平值：不到 1 万保留 2 位并去掉多余的零；1 万起按数字体系缩写，保留 2 位
assert.equal(formatValue("300.00", yuan, "cn"), "300元");
assert.equal(formatValue("24564274.23", yuan, "cn"), "2456.43万元");
assert.equal(formatValue("24564274.23", yuan, "intl"), "24.56M元");
assert.equal(formatValue(123456789, yuan, "cn"), "1.23亿元");
// unit_scale 先换算回基本单位（金额以分存储时 scale=0.01）
assert.equal(formatValue(150000, { kind: "level", unit: "元", scale: 0.01 }, "cn"), "1500元");
// 计数：整数
assert.equal(formatValue(97632, { kind: "count", unit: "件" }, "cn"), "9.76万件");
assert.equal(formatValue(33.4, { kind: "count", unit: "件" }, "cn"), "33件");
// 无单位的比率显示百分比，1 位小数；有单位的比率原值加单位
assert.equal(formatValue("0.0658203931659", { kind: "ratio" }, "cn"), "6.6%");
assert.equal(formatValue("10.6035674585", { kind: "ratio", unit: "元/单" }, "cn"), "10.6元/单");
// 差额、变化率带符号，负号用 U+2212
assert.equal(formatValue("-619439.05", { kind: "delta", unit: "元" }, "cn"), "−61.94万元");
assert.equal(formatValue("195963.09", { kind: "delta", unit: "元" }, "cn"), "+19.60万元");
assert.equal(formatValue("-0.0843", { kind: "rate_change" }, "cn"), "−8.4%");
assert.equal(formatValue("0.0404", { kind: "rate_change" }, "cn"), "+4.0%");
assert.equal(formatValue("0", { kind: "rate_change" }, "cn"), "0.0%");
// 比率之差：个百分点
assert.equal(formatValue("0.012", { kind: "delta", of_ratio: true }, "cn"), "+1.2个百分点");
// 未知量纲：最多 4 位小数、不带单位
assert.equal(formatValue("1.23456", { kind: "unknown", unit: "元" }, "cn"), "1.2346");
assert.equal(formatValue(undefined, yuan, "cn"), "—");
assert.equal(formatValue("华东", yuan, "cn"), "华东");
// 坐标轴：去零、不带单位
assert.equal(formatAxis(20000000, yuan, "cn"), "2000万");
assert.equal(formatAxis(0.065, { kind: "ratio" }, "cn"), "6.5%");
assert.equal(formatAxis(-5000, yuan, "intl"), "−5000");
assert.equal(unitLabel(yuan), "元");
assert.equal(unitLabel({ kind: "ratio" }), "");
assert.equal(toNumber(""), null);
assert.equal(toNumber("1e3"), 1000);
console.log("numbers: ok");
