// Run: node src/pages/catalog/DerivedMetric.test.mjs (from frontend/).
// Exercise the real React page, generated request shape and mutation callbacks without a backend.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const query = require("@tanstack/react-query");
const runtime = require("react/jsx-runtime");
const ts = require("typescript");
const client = new query.QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
const forms = [];
const pending = [];
const requests = [];
const version = {
  id: "derived-v1", metric_id: "average-order", metric_code: "average_order", metric_name: "客单价", version_no: 2,
  kind: "derived", status: "draft", description: "  客单价  ", expression: "  paid_sales / orders_cnt  ",
  components: [
    { metric_id: "paid-sales", metric_code: "paid_sales", metric_name: "实付销售额", version_id: "sales-v3", version_no: 3 },
    { metric_code: "orders_cnt", metric_name: "订单数", version_id: "orders-v2", version_no: 2 },
  ],
  time_column: "", timezone: "Asia/Shanghai", dimensions: [{ name: "region", label: "区域" }],
  joins: [], grain: [], dependency: [], asset_versions: {}, publish_check: {}, created_by: "other-user",
};
const metrics = [{ id: "paid-sales", code: "paid_sales", name: "实付销售额" }, { id: "orders", code: "orders_cnt", name: "订单数" }];
for (const [key, data] of [[["metrics"], metrics], [["settings"], {}], [["users"], []]]) client.setQueryData(key, data);

const jsxRuntime = Object.fromEntries(Object.entries(runtime).map(([name, fn]) => [name,
  typeof fn !== "function" ? fn : (...args) => {
    if (args[0] === "form") forms.push(args[1]);
    return fn(...args);
  },
]));
const queryForSsr = {
  ...query,
  useMutation(options) {
    const currentClient = query.useQueryClient();
    return { isPending: false, mutate(variables, callbacks) {
      pending.push(currentClient.getMutationCache().build(currentClient, options).execute(variables)
        .then((result) => callbacks?.onSuccess?.(result)));
    } };
  },
};
const apiClient = {
  ApiError: class extends Error {}, unwrap: async (response) => response,
  api: Object.fromEntries(["POST", "PUT"].map((method) => [method, async (path, request) => {
    requests.push({ method, path, request });
    return { ...version, description: request.body.description, expression: request.body.expression, publish_check: {} };
  }])),
};
const modules = new Map();
function load(file, extra = "") {
  if (modules.has(file)) return modules.get(file);
  const source = readFileSync(new URL(file, import.meta.url), "utf8") + extra;
  const output = ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const module = { exports: {} };
  const resolve = (name) => {
    if (name === "react/jsx-runtime") return jsxRuntime;
    if (name === "@tanstack/react-query") return queryForSsr;
    if (name.endsWith("api/client") || name === "./client") return apiClient;
    if (name.endsWith("api/hooks")) return load("../../api/hooks.ts");
    if (name.endsWith("components/ui")) return load("../../components/ui.tsx");
    if (name === "./Icon") return { Icon: () => null, Hint: () => null };
    // 界面语言：测试里按简体原文渲染（键即原文）
    if (name.endsWith("/i18n")) return { t: (k, o) => k.replace(/\{\{(\w+)\}\}/g, (_, p) => String(o?.[p] ?? "")), currentLanguage: () => "zh-CN" };
    if (name.endsWith("layout/Shell")) return { useRoles: () => new Set(["admin"]), useMe: () => ({ isSuccess: true, data: { id: "admin" } }) };
    if (name.endsWith("format")) return { formatTime: String };
    if (name === "react-router") return { Link: ({ to, children, ...props }) => React.createElement("a", { ...props, href: to }, children) };
    return require(name);
  };
  new Function("require", "module", "exports", output)(resolve, module, module.exports);
  modules.set(file, module.exports);
  return module.exports;
}
const page = load("./MetricDetail.tsx", "\nexport { VersionBody, VersionEditor, DerivedComponents };\n");
function render(Component, props) {
  return renderToStaticMarkup(React.createElement(query.QueryClientProvider, { client }, React.createElement(Component, props)));
}

const detail = render(page.VersionBody, { v: version, tab: "definition", canEdit: true, onEdit() {} });
assert.match(detail, /公式/);
assert.match(detail, /实付销售额 · V3/);
assert.match(detail, /href="\/catalog\/metrics\/paid-sales"/);
assert.match(detail, /href="\/catalog\/metrics\/orders"/, "Older component records resolve links by metric code");
assert.match(detail, /共有维度/);
assert.match(detail, /区域/);
assert.doesNotMatch(detail, /基础资产|时间字段|统计粒度|undefined/, "Derived dimensions have no source column");
const dependency = render(page.VersionBody, { v: version, tab: "dependency", canEdit: false, onEdit() {} });
assert.match(dependency, /锁定组成版本/);
assert.doesNotMatch(dependency, /依赖与指纹|暂无依赖记录/);
assert.match(render(page.DerivedComponents, { components: [] }), /提交后锁定/);
client.setQueryData(["metrics"], []);
assert.match(render(page.DerivedComponents, { components: version.components }), /href="\/catalog\/metrics\/paid-sales"/, "The locked metric ID works without catalog lookup");
client.setQueryData(["metrics"], metrics);

let saved = 0;
for (const mode of ["new", "edit"]) {
  const editor = render(page.VersionEditor, { metricId: version.metric_id, base: version, mode, onClose() {}, onSaved(id) {
    saved += 1;
    assert.equal(id, version.id);
    assert.equal(client.getQueryData(["metric-version", id]).expression, version.expression.trim());
  } });
  assert.match(editor, /公式/);
  assert.match(editor, /av-mono av-metric-derived__formula-input/);
  assert.match(editor, /paid_sales · 已存在/);
  assert.match(editor, /orders_cnt · 已存在/);
  assert.doesNotMatch(editor, /基础资产|时间字段|允许维度|统计粒度|关联路径/);
  assert.equal(/版本类型/.test(editor), mode === "new", "Existing version kind is fixed");
  forms.at(-1).onSubmit({ preventDefault() {} });
  await Promise.all(pending);
}
assert.equal(saved, 2);
assert.deepEqual(requests.map(({ method }) => method), ["POST", "PUT"]);
assert.equal(requests[0].path, "/api/metrics/{metric_id}/versions");
assert.equal(requests[1].path, "/api/metric-versions/{version_id}");
for (const { request } of requests) {
  assert.deepEqual(request.body, { kind: "derived", description: "客单价", expression: "paid_sales / orders_cnt", unit: null, unit_scale: 1 }, "Never send base-only fields for a derived version (unit is shared: ADR 0009)");
}

const unknown = render(page.VersionEditor, { metricId: version.metric_id, base: { ...version, expression: "paid_sales + missing_metric + paid_sales + 1e3" }, mode: "new", onClose() {}, onSaved() {} });
assert.match(unknown, /missing_metric · 不存在/);
assert.equal((unknown.match(/paid_sales · 已存在/g) ?? []).length, 1, "Repeated references appear once");
assert.doesNotMatch(unknown, /e3 · 不存在/, "Scientific numeric literals are not metric references");
assert.match(unknown, /最终以后端校验为准/);
for (const literal of ["1_000", "1_000.25_0", ".1_25", "1_0e+1_0", "1_0E-1_0", "1_0.e+2", "0x10", "0xDE_AD", "0X_FF", "0o7_7", "0O_77", "0b1_010", "0B_1010"]) {
  const numericEditor = render(page.VersionEditor, {
    metricId: version.metric_id, base: { ...version, expression: `paid_sales / ${literal} + orders_cnt` },
    mode: "new", onClose() {}, onSaved() {},
  });
  assert.doesNotMatch(numericEditor, / · 不存在/, `Python numeric literal ${literal} must not create a metric reference`);
  assert.match(numericEditor, /paid_sales · 已存在/);
  assert.match(numericEditor, /orders_cnt · 已存在/);
}
assert.equal(client.getQueryCache().find({ queryKey: ["assets"] }), undefined, "Derived editing must not depend on source assets");
assert.equal(client.getQueryCache().find({ queryKey: ["datasources"] }), undefined);
assert.equal(client.getQueryCache().find({ queryKey: ["join-paths"] }), undefined);
client.clear();
console.log("Derived metric UI passed: definition, component links, shared dimensions, reference hints, POST/PUT payloads and cache refresh.");
