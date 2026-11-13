// Run: node src/pages/catalog/dossier/dossier.test.mjs (from frontend/).
// 资产档案页的纯函数：表名、来源字段、折叠、说明计数与状态、流向带右栏。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./dossier.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const d = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

// 表名取最后一段；库名是其余部分
assert.equal(d.tableName("cat.db.t_a"), "t_a");
assert.equal(d.tableName("t_a"), "t_a");
assert.equal(d.tableName(""), "");
assert.equal(d.tableName(null), "");
assert.equal(d.schemaOf("cat.db.t_a"), "cat.db");
assert.equal(d.schemaOf("t_a"), "");
assert.equal(d.sourceLabel({ ref: "db.t_a", column: "c1" }), "t_a.c1");
assert.equal(d.sourceLabel({ ref: "db.t_a", column: null }), "t_a");

// 折叠：不超过上限全部显示；超过时显示前 limit 个并给出隐藏数；展开后全部显示
const items = Array.from({ length: 15 }, (_, i) => i);
assert.deepEqual(d.visible(items.slice(0, 12), 12, false), { shown: items.slice(0, 12), hidden: 0 });
assert.deepEqual(d.visible(items, 12, false), { shown: items.slice(0, 12), hidden: 3 });
assert.deepEqual(d.visible(items, 12, true), { shown: items, hidden: 0 });
assert.equal(d.visible(items, 4, false).hidden, 11);

// 说明状态与计数：草稿和过期都算待确认
assert.equal(d.noteState(null), "none");
assert.equal(d.noteState({ status: "stale" }), "stale");
assert.equal(d.needsConfirm({ status: "draft" }), true);
assert.equal(d.needsConfirm({ status: "confirmed" }), false);
assert.equal(d.needsConfirm(undefined), false);
assert.deepEqual(d.noteCounts([{ note: { status: "confirmed" } }, { note: { status: "draft" } }, { note: { status: "stale" } }, { note: null }, {}]),
  { confirmed: 1, pending: 2 });
assert.deepEqual(d.noteCounts([]), { confirmed: 0, pending: 0 });

// 头像字
assert.equal(d.initialOf("林晓"), "林");
assert.equal(d.initialOf(" alice "), "A");
assert.equal(d.initialOf(""), "?");
assert.equal(d.initialOf(null), "?");

// 最近提问的人
const people = [{ id: "a", last_asked_at: "2026-11-01T00:00:00Z" }, { id: "b", last_asked_at: "2026-11-10T00:00:00Z" }, { id: "c", last_asked_at: "2026-11-05T00:00:00Z" }];
assert.equal(d.latestAsker(people).id, "b");
assert.equal(d.latestAsker([]), null);

// 一句话说明：注释优先；说明按状态；没有文字为 none
assert.equal(d.summaryState({ text: "x", source: "comment" }), "comment");
assert.equal(d.summaryState({ text: "x", source: "note", note: { status: "draft" } }), "draft");
assert.equal(d.summaryState({ text: "x", source: "note", note: { status: "stale" } }), "stale");
assert.equal(d.summaryState({ text: "x", source: "note", note: { status: "confirmed" } }), "confirmed");
assert.equal(d.summaryState({ text: null, source: null }), "none");
assert.equal(d.summaryState({}), "none");

// 流向带右栏：为 0 的不显示，三项都没有时 any=false
assert.deepEqual(d.usage({ metrics: [], asked: { total: 0 }, downstream: [] }), { metrics: 0, asked: 0, downstream: 0, any: false });
assert.deepEqual(d.usage({ metrics: [1, 2], asked: { total: 42 } }), { metrics: 2, asked: 42, downstream: 0, any: true });
assert.deepEqual(d.usage({}), { metrics: 0, asked: 0, downstream: 0, any: false });

console.log("dossier: ok");
