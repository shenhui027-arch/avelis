// Run: node src/i18n/render.test.mjs (from frontend/).
// 关键页面三语渲染（ADR 0015）：用 Vite 的 SSR 加载真实组件，按三种语言各渲染登录页、404 页、工作台与用户页，
// 英文里不能残留中文，繁体里不能有常见简体字，简体保持原文。数据全用 ASCII，确保检出的中文只来自界面文案。
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

import { createRequire } from "node:module";

import { createServer } from "vite";

const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query"); // 与 Vite 外部化时同一份 ESM 实例
const { MemoryRouter } = await import("react-router");

const root = fileURLToPath(new URL("../..", import.meta.url));

function browserGlobals(lang) {
  const store = new Map([["avelis.lang", lang]]);
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
  const media = { matches: false, addEventListener() {}, removeEventListener() {} };
  Object.assign(globalThis, {
    localStorage: storage,
    sessionStorage: { ...storage, getItem: () => null },
    document: { documentElement: { lang: "", dataset: {} }, cookie: "", addEventListener() {}, removeEventListener() {} },
    window: { localStorage: storage, matchMedia: () => media, addEventListener() {}, removeEventListener() {}, location: { reload() {} } },
  });
  Object.defineProperty(globalThis, "navigator", { value: { language: "en-US", languages: ["en-US"] }, configurable: true });
}

const WORKBENCH = {
  stages: [{ key: "collect", label: "collect", value: 3, unit: "", status: "ok", note: "ok" }],
  todos: [{ kind: "review", status: "warn", title: "todo-a", meta: "meta-a", ref: "ref-a", link: "/changes", at: "2026-10-30T02:00:00Z" }],
  blocked_answers: [],
  my_metrics: [{ id: "m1", name: "metric_a", version_no: 2, status: "published", grain: ["dim_a"], asks_week: 4 }],
  datasources: [{ id: "d1", name: "source_a", kind: "postgres", interval_minutes: 15, last_run_at: null, status: "never" }],
  cadence: [{ hour: 1, runs: 2, changed: false, future: false }],
  recent_changes: [{ id: "c1", asset: "db.table_a", kind: "column_added", impacts: 2, status: "open", at: "2026-10-30T03:00:00Z" }],
};
const ME = { id: "u1", username: "user_a", display_name: "User A", roles: ["admin"], must_change_password: false };
const USERS = [{ id: "u1", username: "user_a", display_name: "User A", roles: ["admin", "analyst"], domains: [{ id: "d1", name: "domain_a", role: "owner" }],
  is_active: true, must_change_password: false, last_login_at: "2026-10-30T02:00:00Z" }];

async function renderAll(lang) {
  browserGlobals(lang);
  const server = await createServer({ root, logLevel: "error", appType: "custom", server: { middlewareMode: true, hmr: false },
    // React 等依赖用 Node 自己加载的同一份，页面组件与测试共用一个 React 实例
    ssr: { external: ["react", "react-dom", "@tanstack/react-query", "react-router"] } });
  try {
    const { currentLanguage } = await server.ssrLoadModule("/src/i18n/index.ts");
    const { Login } = await server.ssrLoadModule("/src/pages/Login.tsx");
    const { NotFound } = await server.ssrLoadModule("/src/pages/PageState.tsx");
    const { Workbench } = await server.ssrLoadModule("/src/pages/Workbench.tsx");
    const { UsersPage } = await server.ssrLoadModule("/src/pages/settings/UsersPage.tsx");
    assert.equal(currentLanguage(), lang);
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
    client.setQueryData(["workbench"], WORKBENCH);
    client.setQueryData(["me"], ME);
    client.setQueryData(["users"], USERS);
    client.setQueryData(["settings"], { business_timezone: "UTC" });
    const render = (el) => renderToStaticMarkup(React.createElement(QueryClientProvider, { client },
      React.createElement(MemoryRouter, null, React.createElement(el))));
    return { login: render(Login), notFound: render(NotFound), workbench: render(Workbench), users: render(UsersPage) };
  } finally {
    await server.close();
  }
}

// 只看界面上的文字：去掉标签，保留 title / aria-label / placeholder 这些也会被看到或读出的属性；
// 语言选项按各自语言书写（带 lang 属性），不算残留
function visibleText(html) {
  const attrs = [...html.matchAll(/(?:title|aria-label|placeholder)="([^"]*)"/g)].map((m) => m[1]);
  return [html.replace(/<option[^>]*\slang="[^"]*"[^>]*>[^<]*<\/option>/g, "").replace(/<style[\s\S]*?<\/style>/g, "").replace(/<[^>]+>/g, " "), ...attrs].join(" ");
}

const CJK = /[㐀-鿿]/;
const SIMPLIFIED = /[们这对说时间现发经应关数据设页预览类项务认证录户该块问题错误码]/;

const zh = await renderAll("zh-CN");
assert.match(zh.login, /让每一条数据/);
assert.match(zh.workbench, /工作台/);
assert.match(zh.users, /新建用户/);

const tw = await renderAll("zh-TW");
for (const [page, html] of Object.entries(tw)) {
  const m = visibleText(html).match(SIMPLIFIED);
  assert.equal(m, null, `zh-TW ${page} 有简体字“${m?.[0]}”`);
}
assert.match(tw.login, /讓每一筆資料/);

const en = await renderAll("en");
for (const [page, html] of Object.entries(en)) {
  const text = visibleText(html);
  const at = text.search(CJK);
  assert.equal(at, -1, `en ${page} 残留中文：…${text.slice(Math.max(0, at - 30), at + 30)}…`);
}
assert.match(en.login, /Every piece of data/);
assert.match(en.workbench, /2 impacts/); // 复数
assert.match(en.users, /New user/);
console.log("i18n render: ok（登录、404、工作台、用户 × zh-CN / zh-TW / en）");
