// Run: node src/components/tipTarget.test.mjs (from frontend/).
// 提示层借用 title 时控件的可访问名称不能丢（#122）：文字被 display: none 藏起时，名称来自 title，借走期间要补 aria-label。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./tipTarget.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { borrow, giveBack } = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

/** 只模拟用到的 DOM 接口：innerText 是渲染出来的文字，textContent 含隐藏文字。 */
function element({ title, innerText, textContent, ariaLabel }) {
  const attrs = new Map(Object.entries({ title, ...(ariaLabel ? { "aria-label": ariaLabel } : {}) }));
  return {
    dataset: {}, innerText, textContent,
    hasAttribute: (k) => attrs.has(k), getAttribute: (k) => attrs.get(k) ?? null,
    setAttribute: (k, v) => attrs.set(k, v), removeAttribute: (k) => attrs.delete(k),
    attrs,
  };
}
// 可访问名称（简化）：aria-label 优先，其次可见文字，再次 title
const name = (el) => el.attrs.get("aria-label") ?? (el.innerText.trim() || el.attrs.get("title") || "");

// 收起侧边栏：标签文字被隐藏，名称来自 title；提示显示期间名称不变，收起后恢复原状
const collapsed = element({ title: "目录", innerText: "", textContent: "目录" });
assert.equal(name(collapsed), "目录");
const tip = borrow(collapsed);
assert.equal(collapsed.attrs.has("title"), false);
assert.equal(name(collapsed), "目录");
giveBack(tip);
assert.equal(collapsed.attrs.get("title"), "目录");
assert.equal(collapsed.attrs.has("aria-label"), false);

// 展开侧边栏：文字可见，名称来自文字，不补 aria-label
const expanded = element({ title: "目录", innerText: "目录", textContent: "目录" });
borrow(expanded);
assert.equal(expanded.attrs.has("aria-label"), false);
assert.equal(name(expanded), "目录");

// 已有 aria-label：不动它
const labelled = element({ title: "通知", innerText: "", textContent: "", ariaLabel: "通知（3 条未读）" });
giveBack(borrow(labelled));
assert.equal(labelled.attrs.get("aria-label"), "通知（3 条未读）");

// 纯图标按钮：同样补上、收起后撤掉
const icon = element({ title: "下载", innerText: "", textContent: "" });
const iconTip = borrow(icon);
assert.equal(name(icon), "下载");
giveBack(iconTip);
assert.equal(icon.attrs.has("aria-label"), false);

console.log("tipTarget: ok");
