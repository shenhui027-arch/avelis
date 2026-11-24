// Run: node src/components/overlay.test.mjs (from frontend/).
// 浮层栈核心逻辑测试：LIFO Escape 单层关闭、hasOverlayAbove/isTopOverlay、IME 组合态忽略、幂等释放与窗口监听器生命周期。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const listeners = new Map();
globalThis.window = {
  addEventListener(type, fn) {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(fn);
  },
  removeEventListener(type, fn) {
    listeners.get(type)?.delete(fn);
  },
};

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./overlayStack.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { pushOverlay, hasOverlayAbove, isTopOverlay, overlayCount } =
  await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

function fireKey(key, extra = {}) {
  let prevented = false;
  let stopped = false;
  const event = {
    key,
    defaultPrevented: false,
    isComposing: false,
    keyCode: key === "Escape" ? 27 : 0,
    preventDefault() { prevented = true; this.defaultPrevented = true; },
    stopPropagation() { stopped = true; },
    ...extra,
  };
  for (const fn of [...(listeners.get("keydown") ?? [])]) fn(event);
  return { prevented, stopped };
}

assert.equal(overlayCount(), 0);
assert.equal(listeners.get("keydown")?.size ?? 0, 0);

// 1. 栈顺序：Drawer -> ConfirmDialog，按一次 Esc 只关 ConfirmDialog，再按一次关 Drawer
const closed = [];
let dialog = null;
const drawer = pushOverlay(() => closed.push("drawer"));
assert.equal(overlayCount(), 1);
assert.equal(listeners.get("keydown")?.size, 1);
assert.equal(isTopOverlay(drawer.id), true);
assert.equal(hasOverlayAbove(drawer.id), false);

dialog = pushOverlay(() => {
  closed.push("dialog");
  dialog.release();
});
assert.equal(overlayCount(), 2);
assert.equal(listeners.get("keydown")?.size, 1, "只挂载一个共享的 window keydown 监听器");
assert.equal(hasOverlayAbove(drawer.id), true);
assert.equal(isTopOverlay(drawer.id), false);
assert.equal(hasOverlayAbove(dialog.id), false);
assert.equal(isTopOverlay(dialog.id), true);

// IME 组合输入中按 Esc 不得关闭任何浮层
const ime = fireKey("Escape", { isComposing: true, keyCode: 229 });
assert.equal(ime.prevented, false);
assert.deepEqual(closed, []);

// 已被其它内联控件消费（defaultPrevented）的 Esc 不触发浮层关闭
const handled = fireKey("Escape", { defaultPrevented: true });
assert.deepEqual(closed, []);

// 非 Escape 键忽略
fireKey("Enter");
assert.deepEqual(closed, []);

// 第一次 Esc：只关顶层 dialog
const firstEsc = fireKey("Escape");
assert.equal(firstEsc.prevented, true);
assert.equal(firstEsc.stopped, true);
assert.deepEqual(closed, ["dialog"]);
assert.equal(overlayCount(), 1);
assert.equal(hasOverlayAbove(drawer.id), false);
assert.equal(isTopOverlay(drawer.id), true);

// 幂等 release 不会误删其它层
dialog.release();
assert.equal(overlayCount(), 1);

// 第二次 Esc：关 drawer 并自动卸载监听器
const secondEsc = fireKey("Escape");
assert.equal(secondEsc.prevented, true);
assert.deepEqual(closed, ["dialog", "drawer"]);
drawer.release();
assert.equal(overlayCount(), 0);
assert.equal(listeners.get("keydown")?.size, 0);

// 3. 中间层卸载不影响顶层归属
const a = pushOverlay(() => closed.push("a"));
const b = pushOverlay(() => closed.push("b"));
const c = pushOverlay(() => closed.push("c"));
assert.equal(hasOverlayAbove(a.id), true);
assert.equal(hasOverlayAbove(b.id), true);
assert.equal(isTopOverlay(c.id), true);
b.release();
assert.equal(overlayCount(), 2);
assert.equal(hasOverlayAbove(a.id), true);
assert.equal(isTopOverlay(c.id), true);
c.release();
assert.equal(isTopOverlay(a.id), true);
a.release();
assert.equal(overlayCount(), 0);

console.log("overlay: ok");
