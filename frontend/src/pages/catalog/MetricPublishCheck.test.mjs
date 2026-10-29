// Run: node src/pages/catalog/MetricPublishCheck.test.mjs (from frontend/).
// Render the real page and execute its save callback without a browser or backend.
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
const keys = [];
const forms = [];
const mutations = [];
const trace = [];
const original = {
  id: "version-1", metric_id: "metric-1", metric_code: "revenue", metric_name: "收入", version_no: 1,
  status: "draft", description: "收入", expression: "SUM(amount)", base_asset: { id: "asset-1", qualified: "db.public.orders" },
  time_column: "created_at", timezone: "Asia/Shanghai", dimensions: [], joins: [], grain: [], dependency: [], asset_versions: {},
  publish_check: { ok: true, checked_at: "2026-10-29T00:00:00Z", blockers: [], warnings: [], joins: [] }, created_by: "user-1",
};
const saved = { ...original, expression: "SUM(net_amount)", publish_check: {} };
const metric = { id: "metric-1", code: "revenue", name: "收入", aliases: [], owner: { display_name: "管理员" }, versions: [{ id: original.id, version_no: 1, status: "draft" }] };
for (const [key, data] of [
  [["metric-version", original.id], original], [["settings"], {}], [["users"], []], [["join-paths"], []],
  [["assets"], [{ id: "asset-1", qualified: "db.public.orders", datasource: { id: "source-1" } }]],
  [["datasources"], [{ id: "source-1", engine: { capabilities: ["query"] } }]], [["asset", "asset-1"], { columns: [] }],
]) client.setQueryData(key, data);

const jsxRuntime = Object.fromEntries(Object.entries(runtime).map(([name, fn]) => [name,
  typeof fn !== "function" ? fn : (...args) => {
    const [type, props, key] = args;
    if (type?.name === "VersionBody") keys.push(key);
    if (type === "form") forms.push(props);
    return fn(...args);
  },
]));
const modules = new Map();
const queryForSsr = {
  ...query,
  // SSR has no mutation-observer subscription; use the real mutation cache to
  // deliver both hook and per-call callbacks, in the same order as the browser.
  useMutation(options) {
    const currentClient = query.useQueryClient();
    return { isPending: false, mutate(variables, callbacks) {
      const pending = currentClient.getMutationCache().build(currentClient, options).execute(variables)
        .then((result) => callbacks?.onSuccess?.(result));
      mutations.push(pending);
    } };
  },
};
const apiClient = {
  ApiError: class extends Error {}, unwrap: async (response) => response,
  api: { PUT: async (path, request) => {
    assert.equal(path, "/api/metric-versions/{version_id}");
    assert.equal(request.params.path.version_id, original.id);
    assert.equal(request.body.expression, saved.expression);
    trace.push("put");
    return saved;
  } },
};
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
    if (name.endsWith("layout/Shell")) return { useRoles: () => new Set(["admin"]), useMe: () => ({ isSuccess: true, data: { id: "admin-1" } }) };
    if (name.endsWith("format")) return { formatTime: String };
    if (name === "react-router") return { Link: ({ to, children, ...props }) => React.createElement("a", { ...props, href: to }, children) };
    return require(name);
  };
  new Function("require", "module", "exports", output)(resolve, module, module.exports);
  modules.set(file, module.exports);
  return module.exports;
}
const page = load("./MetricDetail.tsx", "\nexport { MetricContent, VersionEditor };\n");
function render(Component, props) {
  return renderToStaticMarkup(React.createElement(query.QueryClientProvider, { client }, React.createElement(Component, props)));
}

const before = render(page.MetricContent, { metric });
assert.match(before, /<h2[^>]*>发布前检查 .*?通过/, "The original draft must start with a passed check");
client.setQueryData(["metric-version", original.id], saved);
render(page.MetricContent, { metric });
client.setQueryData(["metric-version", original.id], structuredClone(saved));
render(page.MetricContent, { metric });
assert.notEqual(keys[0], keys[1], "Editing the same version must remount VersionBody and clear its previous mutation result");
assert.equal(keys[1], keys[2], "An unchanged refetch must preserve the current check result");

client.setQueryData(["metric-version", original.id], original);
const invalidate = client.invalidateQueries.bind(client);
client.invalidateQueries = (...args) => { trace.push("invalidate"); return invalidate(...args); };
render(page.VersionEditor, { metricId: metric.id, base: saved, mode: "edit", onClose() {}, onSaved(id) {
  assert.equal(id, saved.id);
  assert.deepEqual(client.getQueryData(["metric-version", saved.id]), saved, "The PUT response must replace cached publish_check before closing the editor");
  trace.push("saved");
} });
forms.at(-1).onSubmit({ preventDefault() {} });
await Promise.all(mutations);
assert.equal(mutations.length, 1, "The actual editor save action must execute");
assert.equal(trace[0], "put");
assert.ok(trace.includes("invalidate"), "Exercise the real useAction invalidation before its local save callback");
assert.equal(trace.at(-1), "saved");
const html = render(page.MetricContent, { metric });
assert.doesNotMatch(html, /<h2[^>]*>发布前检查/, "The saved draft must not display the previous passed check");
assert.equal(keys.at(-1), keys[1]);
client.clear();
console.log("Metric publish-check regression passed: same-id edit clears old check; unchanged refetch preserves it; PUT response replaces cache after invalidation.");
