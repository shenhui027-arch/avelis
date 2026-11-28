// Run: node src/pages/lineage/edgeBadges.test.mjs (from frontend/).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./edgeBadges.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const m = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

const col = (source_column, target_column, relation, expression = null) => ({ source_column, target_column, relation, expression, origin: "parsed", attributes: {} });

// 只有直接传值 / 表级 / 折叠边：不加徽章
assert.equal(m.edgeBadge([col("a", "a", "value_direct"), col(null, null, "table")]), null);
assert.equal(m.edgeBadge([col(null, null, "trace")]), null);
assert.equal(m.edgeBadge([]), null);

// 混合：徽章按 聚合、计算、过滤、关联键、分组 排序去重；明细先按徽章顺序再按原顺序，直接传值不列
const mixed = [
  col("k", null, "join_key", "o.k = s.k"),
  col("a", "a", "value_direct"),
  col("p", "amt", "value_computed", "p * q"),
  col("x", null, "filter", "x > 0"),
  col("v", "total", "value_aggregated", "SUM(v)"),
  col("q", "amt2", "value_computed", "q + 1"),
];
let b = m.edgeBadge(mixed);
assert.deepEqual(b.kinds, ["aggregated", "computed", "filter", "join_key"]);
assert.deepEqual(b.lines.map((l) => [l.kind, l.source, l.target, l.expression]), [
  ["aggregated", "v", "total", "SUM(v)"],
  ["computed", "p", "amt", "p * q"],
  ["computed", "q", "amt2", "q + 1"],
  ["filter", "x", null, "x > 0"],
  ["join_key", "k", null, "o.k = s.k"],
]);
assert.equal(b.more, 0);
assert.equal(b.influenceOnly, false);

// 只有影响条件
b = m.edgeBadge([col("x", null, "filter", "x > 0"), col("g", null, "group_by")]);
assert.deepEqual(b.kinds, ["filter", "group_by"]);
assert.equal(b.influenceOnly, true);

// 超过 max：只列前 max 条，more 记剩余
b = m.edgeBadge(Array.from({ length: 9 }, (_, i) => col(`s${i}`, `t${i}`, "value_computed", `e${i}`)), 6);
assert.equal(b.lines.length, 6);
assert.equal(b.more, 3);
assert.deepEqual(b.kinds, ["computed"]);

// 提示文字：每行 符号 源 → 目标：表达式；缺目标、缺源、缺表达式的写法；超出时末行
const tip = m.badgeTip({
  kinds: ["aggregated", "filter", "group_by"],
  lines: [
    { kind: "aggregated", source: "v", target: "total", expression: "SUM(v)" },
    { kind: "filter", source: "x", target: null, expression: "x > 0" },
    { kind: "group_by", source: null, target: null, expression: null },
  ],
  more: 2,
  influenceOnly: false,
}, "（影响）", (n) => `还有 ${n} 条`);
assert.equal(tip, ["Σ v → total：SUM(v)", "⊳ x → （影响）：x > 0", "▦ — → （影响）", "还有 2 条"].join("\n"));
assert.equal(m.badgeTip({ kinds: ["computed"], lines: [{ kind: "computed", source: "a", target: "b", expression: null }], more: 0, influenceOnly: false }, "（影响）", () => "x"), "ƒ a → b");

console.log("edgeBadges: all assertions passed");
