// Run: node src/pages/lineage/trace.test.mjs (from frontend/).
// 血缘追溯契约：只沿数据流走（过滤 / 关联键不参与）、深度裁剪不影响端点、环不死循环、外部表身份映射可追溯、表格行与导出。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./trace.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const m = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

const node = (id, qualified, kind = "table") => ({ id, qualified, kind, status: "active", grains: [], in_scope: true });
const col = (source_column, target_column, relation, expression = null) => ({ source_column, target_column, relation, expression, origin: "parsed", attributes: {} });
const edge = (source, target, columns, relations = [...new Set(columns.map((c) => c.relation))]) => ({
  source, source_ref: source.replace(/^ref:/, ""), target, columns, relations, origins: ["parsed"],
});
// A → B → C → D；C ⇄ E 成环；X 只以过滤条件影响 B；设计时表 ref:dw.tmp 表级流向 D；外部表 EXT 与真实表 A 是同一张表
const graph = {
  nodes: [node("A", "sr.ods.a"), node("B", "sr.dw.b"), node("C", "sr.dm.c"), node("D", "sr.rpt.d"), node("E", "sr.dm.e"),
    node("X", "sr.dim.x"), node("EXT", "sr.ext.a", "external_table"), node("ref:dw.tmp", "dw.tmp", "design_time")],
  edges: [
    edge("A", "B", [col("a1", "b1", "value_direct"), col("a2", "b2", "value_computed", "a2 * 100")]),
    edge("X", "B", [col("x1", null, "filter")]),
    edge("B", "C", [col("b1", "c1", "value_aggregated", "SUM(b1)"), col("b2", "c2", "value_direct")]),
    edge("C", "D", [col("c1", "d1", "value_direct")]),
    edge("C", "E", [col("c3", "e1", "value_direct")]),
    edge("E", "C", [col("e1", "c3", "value_direct")]),
    edge("ref:dw.tmp", "D", [], ["table"]),
  ],
  identity_links: [{ id: "L1", status: "approved", method: "auto", evidence: {}, column_match: { matched: ["a1"] }, source: "sr.ext.a",
    target: { id: "A", qualified: "sr.ods.a" }, created_at: "2026-11-27T00:00:00Z" }],
  joins: [],
};
const sorted = (set) => [...set].sort();
const entries = (map) => Object.fromEntries([...map].sort());
const index = m.buildFlowIndex(graph);

// 表级、双向、走到底：过滤边 X→B 不算上游；环 C⇄E 不死循环
let r = m.trace(index, "B", { level: "table", direction: "both", depth: null });
assert.deepEqual(sorted(r.nodes), ["A", "B", "C", "D", "E"]);
assert.deepEqual(sorted(r.edgeKeys), ["A->B", "B->C", "C->D", "C->E", "E->C"]);
assert.deepEqual(r.reach, { up: 1, down: 2 });
assert.deepEqual(entries(r.sources), { A: 1 });
assert.deepEqual(entries(r.sinks), { D: 2 }); // E 还能走回 C，不是终点
assert.equal(r.cols.size, 0);

// 深度 1：只裁剪路径，不影响可达层数与端点
r = m.trace(index, "B", { level: "table", direction: "both", depth: 1 });
assert.deepEqual(sorted(r.nodes), ["A", "B", "C"]);
assert.deepEqual(sorted(r.edgeKeys), ["A->B", "B->C"]);
assert.deepEqual(r.reach, { up: 1, down: 2 });
assert.deepEqual(entries(r.sinks), { D: 2 });

// 只向上游：设计时表也是来源；层数按最短路径；未请求的方向为空
r = m.trace(index, "D", { level: "table", direction: "up", depth: null });
assert.deepEqual(sorted(r.nodes), ["A", "B", "C", "D", "E", "ref:dw.tmp"]);
assert.deepEqual(entries(r.sources), { A: 3, "ref:dw.tmp": 1 });
assert.equal(r.sinks.size, 0);
assert.deepEqual(r.reach, { up: 3, down: 0 });

// 身份映射：真实表 A 的下游含外部表 EXT（边键 id:L1）
r = m.trace(index, "A", { level: "table", direction: "down", depth: null });
assert.ok(r.edgeKeys.has("id:L1"));
assert.deepEqual(entries(r.sinks), { D: 3, EXT: 1 });
assert.equal(r.sources.size, 0);

// 字段级：只走取值关系；过滤字段 x1 不在路径上
r = m.trace(index, "B::b1", { level: "column", direction: "both", depth: null });
assert.deepEqual(sorted(r.cols), ["A::a1", "B::b1", "C::c1", "D::d1"]);
assert.deepEqual(sorted(r.nodes), ["A", "B", "C", "D"]);
assert.deepEqual(sorted(r.edgeKeys), ["A->B", "B->C", "C->D"]);
assert.deepEqual(entries(r.sources), { "A::a1": 1 });
assert.deepEqual(entries(r.sinks), { "D::d1": 2 });
r = m.trace(index, "A::a1", { level: "column", direction: "down", depth: null });
assert.deepEqual(entries(r.sinks), { "D::d1": 3, "EXT::a1": 1 }); // 外部表按 column_match 的同名列接续
r = m.trace(index, "B::b1", { level: "column", direction: "up", depth: 0 });
assert.deepEqual(sorted(r.cols), ["B::b1"]);
assert.deepEqual(entries(r.sources), { "A::a1": 1 });

// 起点没有任何数据流：端点为空，路径只有起点
r = m.trace(index, "X", { level: "table", direction: "both", depth: null });
assert.deepEqual(sorted(r.nodes), ["X"]);
assert.equal(r.sources.size + r.sinks.size + r.edgeKeys.size, 0);

// 只看两端（表级）：保留 A、B、D，原顺序；中间层折叠为 trace 边并记层数
r = m.trace(index, "B", { level: "table", direction: "both", depth: null });
let ends = m.endsGraph(graph, "B", r, "table");
assert.deepEqual(ends.nodes.map((n) => n.id), ["A", "B", "D"]);
assert.deepEqual(ends.identity_links, []);
assert.deepEqual(ends.joins, []);
assert.deepEqual(ends.edges.map((e) => [e.source, e.target, e.relations, e.origins, e.columns.map((c) => c.attributes.hops)]),
  [["A", "B", [m.TRACE_RELATION], [m.TRACE_RELATION], [1]], ["B", "D", [m.TRACE_RELATION], [m.TRACE_RELATION], [2]]]);
assert.equal(ends.edges[0].columns[0].source_column, null);

// 只看两端（字段级）：端点字段与起点字段配对
r = m.trace(index, "B::b1", { level: "column", direction: "both", depth: null });
ends = m.endsGraph(graph, "B::b1", r, "column");
assert.deepEqual(ends.nodes.map((n) => n.id), ["A", "B", "D"]);
assert.deepEqual(ends.edges.map((e) => [e.source, e.target, e.columns.map((c) => [c.source_column, c.target_column, c.relation, c.attributes.hops])]),
  [["A", "B", [["a1", "b1", m.TRACE_RELATION, 1]]], ["B", "D", [["b1", "d1", m.TRACE_RELATION, 2]]]]);

// 表格行：每条列级边一行，表级边一行，身份映射一行排最后；设计时表去掉 ref: 前缀
const rows = m.edgeRows(graph);
assert.equal(rows.length, 10);
assert.equal(new Set(rows.map((x) => x.key)).size, 10);
assert.deepEqual(rows[0], { key: rows[0].key, edge: "A->B", sourceId: "A", source: "sr.ods.a", sourceColumn: "a1", targetId: "B",
  target: "sr.dw.b", targetColumn: "b1", relation: "value_direct", expression: null, origin: "parsed" });
const tableRow = rows.find((x) => x.sourceId === "ref:dw.tmp");
assert.deepEqual([tableRow.source, tableRow.sourceColumn, tableRow.targetColumn, tableRow.relation], ["dw.tmp", null, null, "table"]);
assert.deepEqual([rows.at(-1).edge, rows.at(-1).sourceId, rows.at(-1).targetId, rows.at(-1).relation, rows.at(-1).sourceColumn],
  ["id:L1", "A", "EXT", m.IDENTITY_RELATION, null]);
assert.deepEqual(m.edgeRows(graph, new Set(["A->B", "id:L1"])).map((x) => x.edge), ["A->B", "A->B", "id:L1"]);
// 字段级“选中路径”：同一对表之间与所选字段无关的映射（a2→b2、b2→c2）、表级行与影响条件都不算
r = m.trace(index, "B::b1", { level: "column", direction: "both", depth: null });
assert.deepEqual(m.edgeRows(graph, r.edgeKeys, r.cols).map((x) => `${x.sourceId}.${x.sourceColumn}>${x.targetId}.${x.targetColumn}`),
  ["A.a1>B.b1", "B.b1>C.c1", "C.c1>D.d1"]);
assert.equal(m.edgeRows(graph, r.edgeKeys, new Set()).length, 5); // 空集合等同于只按边过滤

// 筛选：不分大小写，匹配表名、字段、表达式；按关系筛
assert.deepEqual(m.filterRows(rows, { text: "B2", relation: "" }).map((x) => `${x.sourceColumn}>${x.targetColumn}`), ["a2>b2", "b2>c2"]);
assert.equal(m.filterRows(rows, { text: "sum(", relation: "" }).length, 1);
assert.equal(m.filterRows(rows, { text: "RPT", relation: "" }).length, 2);
assert.deepEqual(m.filterRows(rows, { text: "", relation: "filter" }).map((x) => x.sourceColumn), ["x1"]);
assert.equal(m.filterRows(rows, { text: "", relation: "" }).length, 10);

// 排序：稳定，null 升降序都排最后，不改入参
const before = rows.map((x) => x.key);
const asc = m.sortRows(rows, "targetColumn", "asc").map((x) => x.targetColumn);
const desc = m.sortRows(rows, "targetColumn", "desc").map((x) => x.targetColumn);
assert.deepEqual(asc.slice(0, 7), ["b1", "b2", "c1", "c2", "c3", "d1", "e1"]);
assert.deepEqual(asc.slice(7), [null, null, null]);
assert.deepEqual(desc.slice(0, 7), ["e1", "d1", "c3", "c2", "c1", "b2", "b1"]);
assert.deepEqual(desc.slice(7), [null, null, null]);
assert.deepEqual(rows.map((x) => x.key), before);
const bySource = m.sortRows(rows, "source", "asc");
assert.deepEqual(bySource.filter((x) => x.sourceId === "A").map((x) => x.key), rows.filter((x) => x.sourceId === "A").map((x) => x.key)); // 稳定

// 导出二维表：表头 + 行，关系与来源转显示文字，null 为空串
const matrix = m.rowsToMatrix(rows.slice(0, 2), ["源表", "源字段", "目标表", "目标字段", "关系", "表达式", "来源"],
  { relation: (x) => `R:${x}`, origin: (x) => `O:${x}` });
assert.deepEqual(matrix, [
  ["源表", "源字段", "目标表", "目标字段", "关系", "表达式", "来源"],
  ["sr.ods.a", "a1", "sr.dw.b", "b1", "R:value_direct", "", "O:parsed"],
  ["sr.ods.a", "a2", "sr.dw.b", "b2", "R:value_computed", "a2 * 100", "O:parsed"],
]);

console.log("trace: all assertions passed");
