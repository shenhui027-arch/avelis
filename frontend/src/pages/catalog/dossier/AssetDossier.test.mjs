// Run: node src/pages/catalog/dossier/AssetDossier.test.mjs (from frontend/).
// 资产档案页各状态分支的服务端渲染：数据齐全、全空、视图三种情形，不需要后端。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
globalThis.document ??= { body: {} };
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const query = require("@tanstack/react-query");
const ts = require("typescript");
const client = new query.QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
client.setQueryData(["settings"], {});

const apiClient = { ApiError: class extends Error {}, unwrap: async (r) => r, api: {} };
const changeRecords = {
  EVENT_LABEL: { column_added: "新增列" },
  Actor: ({ e }) => React.createElement("span", null, e.actor_db_user ?? "未知"),
  recordLook: () => ["plus", "ok"],
  recordDetail: (e) => String(e.after?.data_type ?? ""),
};
const modules = new Map();
function load(file) {
  if (modules.has(file)) return modules.get(file);
  const source = readFileSync(new URL(file, import.meta.url), "utf8");
  const output = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  const resolve = (name) => {
    if (name.endsWith("api/client") || name === "./client") return apiClient;
    if (name.endsWith("api/hooks")) return load("../../../api/hooks.ts");
    if (name.endsWith("components/ui")) return load("../../../components/ui.tsx");
    if (name === "./controls") return load("../../../components/controls.tsx");
    if (name === "./Icon") return { Icon: () => null, Hint: ({ text }) => React.createElement("i", { "data-hint": text }) };
    if (name.endsWith("changes/ChangeRecords")) return changeRecords;
    if (name.endsWith("/i18n")) return { currentLanguage: () => "zh-CN",
      t: (k, o = {}) => Object.entries(o).reduce((s, [p, v]) => s.split("{{" + p + "}}").join(String(v)), k) };
    if (name === "react-dom") return { ...require("react-dom"), createPortal: (node) => node };
    if (name.endsWith("format")) return { formatTime: String, formatShortTime: (s) => `T(${s})`, formatDate: (s) => `D(${s})` };
    if (name === "react-router") return { Link: ({ to, children, ...props }) => React.createElement("a", { ...props, href: to }, children) };
    if (name.startsWith("./")) return load(`./${name.slice(2)}${name.endsWith("dossier") ? ".ts" : ".tsx"}`);
    return require(name);
  };
  new Function("require", "module", "exports", output)(resolve, module, module.exports);
  modules.set(file, module.exports);
  return module.exports;
}
const { AssetDossier } = load("./AssetDossier.tsx");
const { FlowBand } = load("./FlowBand.tsx");
const render = (Component, props) => renderToStaticMarkup(React.createElement(query.QueryClientProvider, { client }, React.createElement(Component, props)));
const ok = (data) => ({ isPending: false, isError: false, data });

const asset = {
  id: "a1", qualified: "ads.t_daily", kind: "table", status: "active", columns: [{ name: "c0" }],
  datasource: { id: "ds", name: "ds", kind: "starrocks", is_test_adapter: false }, version: { version_no: 3 },
  definition: null, linked_from: [],
};
const note = (status, extra = {}) => ({ id: `n-${status}`, text: `说明-${status}`, status, origin: "model", updated_at: "2026-11-01T00:00:00Z", ...extra });
const fields = Array.from({ length: 14 }, (_, i) => ({ column: `c${i}`, comment: `注释${i}`, relation: "value_direct", sources: [{ asset_id: "up1", ref: "dw.t_up1", column: `s${i}` }], expression: `t.s${i}` }));
fields[0] = { ...fields[0], relation: "value_aggregated", note: note("confirmed", { confirmed_by: { id: "u1", display_name: "甲" } }), sources: [{ asset_id: null, ref: "dw.hidden_t", column: "x" }] };
fields[1] = { ...fields[1], note: note("draft") };
fields[2] = { ...fields[2], note: note("stale"), expression: null };
const full = {
  asset, summary: { text: "一句话草稿", source: "note", note: note("draft") }, owner: { id: "o1", display_name: "乙" }, column_count: 14,
  upstream: [
    ...Array.from({ length: 5 }, (_, i) => ({ asset_id: `up${i}`, ref: `dw.t_up${i}`, comment: `上游${i}`, columns: 3 })),
    { asset_id: null, ref: "dw.hidden_t", comment: null, columns: 0 },
  ],
  downstream: [{ asset_id: "d1", ref: "rpt.t_down" }, { asset_id: null, ref: "rpt.t_down2" }],
  metrics: [{ id: "m1", code: "m_one", name: "口径一", version_id: "mv1", version_no: 1, status: "published", description: "说明一", role: "base" }],
  asked: { days: 30, total: 42, people: [
    { user: { id: "u1", display_name: "甲" }, count: 9, last_asked_at: "2026-11-10T00:00:00Z", last_question: null },
    { user: { id: "u2", display_name: "丙" }, count: 2, last_asked_at: "2026-11-12T00:00:00Z", last_question: "最近的问题" },
  ] },
  produced: { kind: "script", write_mode: "overwrite", scripts: [{ job_id: "j1", filename: "load_t.sql", line_start: 3, line_end: 20, sql: null, imported_at: "2026-11-01T00:00:00Z" }] },
  fields, can_edit_notes: true, model_available: true,
};
client.setQueryData(["change-events", "asset", "a1", 5], { total: 1, items: [{ id: "e1", kind: "column_added", column: "c13", before: {}, after: { data_type: "int" }, actor_db_user: "etl", detected_at: "2026-11-02T00:00:00Z" }] });

// 流向带：上游最多 4 个、其余收起；无权查看的表不可点；写入方式与草稿说明；右栏三项
const band = render(FlowBand, { a: asset, d: full, onMessage() {} });
assert.match(band, /从这 6 张表来/);
assert.equal((band.match(/href="\/catalog\/assets\/up\d"/g) ?? []).length, 4);
assert.match(band, /另有 2 张/);
assert.match(band, /StarRocks/);
assert.match(band, /库 ads/);
assert.match(band, /每次整表覆盖<i data-hint="从导入的脚本推断"/);
assert.match(band, /t_daily/);
assert.match(band, /一句话草稿/);
assert.match(band, /模型草稿 · 待确认/);
assert.match(band, />确认</);
assert.match(band, />改一下</);
assert.match(band, /乙 负责/);
assert.match(band, /14 个字段/);
assert.match(band, /1 个口径/);
assert.match(band, /近 30 天被问 42 次/);
assert.match(band, /最近：丙 · T\(2026-11-12T00:00:00Z\)/);
assert.match(band, /下游 2 张表/);
assert.doesNotMatch(band, /没有采集到上游表|还没有被口径/);

// 档案页签：三张卡、字段血缘（前 12 个 + 展开）、说明各状态、生成按钮、脚本原文不可见时的提示
const tab = render(AssetDossier, { a: asset, dossier: ok(full) });
assert.match(tab, /在用的口径/);
assert.match(tab, /href="\/catalog\/metrics\/m1"/);
assert.match(tab, /最近谁问过/);
assert.match(tab, /近 30 天 42 次/);
assert.match(tab, /最近的问题/, "本人可见的最近问题替代次数");
assert.match(tab, /问过 9 次/);
assert.match(tab, /最近结构变化/);
assert.match(tab, /href="\/changes\?view=records"/);
assert.match(tab, /每个字段怎么来的/);
assert.match(tab, /1 已确认/);
assert.match(tab, /2 待确认/);
assert.match(tab, /让模型补写说明/);
assert.match(tab, /甲 已确认/);
assert.match(tab, /来源变了 · 需重新确认/);
assert.match(tab, /还没有说明/);
assert.match(tab, />写说明</);
assert.match(tab, /汇总|聚合/);
assert.match(tab, /hidden_t\.x/);
assert.doesNotMatch(tab, /href="\/catalog\/assets\/null"/);
assert.equal((tab.match(/<tr>/g) ?? []).length, 13, "表头 1 行 + 前 12 个字段");
assert.match(tab, /显示全部 14 个字段/);
assert.match(tab, /看表达式/);
assert.match(tab, /由脚本 load_t\.sql（第 3–20 行）写入/);
assert.match(tab, /每次整表覆盖写入。/);
assert.match(tab, /脚本原文只对这张表的负责人和维护者显示/);
assert.doesNotMatch(tab, /看脚本|undefined/);

// 无编辑权限：不出现确认、改一下、写说明与生成按钮
const readonly = render(AssetDossier, { a: asset, dossier: ok({ ...full, can_edit_notes: false }) });
assert.doesNotMatch(readonly, /让模型补写说明|>确认<|>改一下<|>写说明</);
assert.doesNotMatch(render(FlowBand, { a: asset, d: { ...full, can_edit_notes: false }, onMessage() {} }), />确认<|>改一下</);

// 全空：流向带两侧给一句话；档案页签不渲染卡片、字段表与产出
client.setQueryData(["change-events", "asset", "a2", 5], { total: 0, items: [] });
const bare = { asset, summary: { text: null, source: null }, asked: { days: 30, total: 0, people: [] }, can_edit_notes: false };
const emptyBand = render(FlowBand, { a: { ...asset, id: "a2" }, d: bare, onMessage() {} });
assert.match(emptyBand, /没有采集到上游表/);
assert.match(emptyBand, /还没有被口径、提问或下游表用到/);
assert.doesNotMatch(emptyBand, /写一句说明|每次|负责/);
assert.match(render(FlowBand, { a: { ...asset, id: "a2" }, d: { ...bare, can_edit_notes: true }, onMessage() {} }), /写一句说明/);
const emptyTab = render(AssetDossier, { a: { ...asset, id: "a2" }, dossier: ok(bare) });
assert.doesNotMatch(emptyTab, /av-dossier-cards|每个字段怎么来的|它是怎么产出的/);
assert.match(emptyTab, /还没有采集到这张表的字段来源、口径或提问记录/);

// 视图：显示“由定义实时算出”和收起的视图 SQL；注释来源的说明不标状态
const view = { ...bare, summary: { text: "注释原文", source: "comment" }, produced: { kind: "view", write_mode: null, scripts: [] } };
const viewTab = render(AssetDossier, { a: { ...asset, id: "a2", kind: "view", definition: { raw_sql: "SELECT 1", canonical_hash: "h" } }, dossier: ok(view) });
assert.match(viewTab, /这是一个视图，内容由定义实时算出/);
assert.match(viewTab, /看视图 SQL/);
const viewBand = render(FlowBand, { a: { ...asset, id: "a2", kind: "view" }, d: view, onMessage() {} });
assert.match(viewBand, /注释原文/);
assert.match(viewBand, /视图/);
assert.doesNotMatch(viewBand, /待确认|需重新确认|每次/);

console.log("asset dossier render: ok（数据齐全、只读、全空、视图）");
