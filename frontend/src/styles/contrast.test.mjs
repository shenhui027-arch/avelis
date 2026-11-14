// Run: node src/styles/contrast.test.mjs (from frontend/).
// issue 132：蓝底上的小字在整段渐变上都要达到 WCAG AA 4.5:1。色值从样式与令牌文件里读，改色后这里会拦住。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
const lum = (c) => c.map((v) => v / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
  .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
const mix = (a, b, share) => a.map((v, i) => v * share + b[i] * (1 - share));
/** 文字（可带透明度）在渐变两端与中间各点上的最低对比度。 */
const worst = (text, alpha, from, to) => Math.min(...[0, 0.25, 0.5, 0.75, 1].map((t) => {
  const bg = mix(to, from, t);
  return contrast(mix(text, bg, alpha), bg);
}));
const WHITE = [255, 255, 255];

const kit = read("./kit.css");
const from = rgb(kit.match(/--hero-from:\s*(#[0-9a-f]{6})/i)[1]);
const to = rgb(kit.match(/--hero-to:\s*(#[0-9a-f]{6})/i)[1]);
const heroRules = kit.slice(kit.indexOf(".av-hero-card {"), kit.indexOf(".av-hero-card .av-heat {"));
const alphas = [...heroRules.matchAll(/color: rgba\(255, 255, 255, ([.\d]+)\)/g)].map((m) => Number(m[1]));
const axis = Number(kit.match(/\.av-hero-card \.av-cadence-axis \{[^}]*rgba\(255, 255, 255, ([.\d]+)\)/)[1]);
assert.ok(alphas.length >= 3, "采集卡片的小字颜色没读到");
for (const alpha of [...alphas, axis, 1]) {
  assert.ok(worst(WHITE, alpha, from, to) >= 4.5, `蓝底卡片上 ${alpha} 透明度的白字对比度不足 4.5:1`);
}

// 档案页主卡沿用同一渐变：带整体透明度的文字同样校验
const dossier = read("./dossier.css");
const faded = [...dossier.matchAll(/\.av-dossier-hero[^{]*\{[^}]*opacity: ([.\d]+)/g)].map((m) => Number(m[1]));
assert.ok(faded.length >= 1, "档案页主卡的透明度没读到");
for (const alpha of faded) {
  assert.ok(worst(WHITE, alpha, from, to) >= 4.5, `档案页主卡 ${alpha} 透明度的文字对比度不足 4.5:1`);
}

// 登录按钮：按钮色 → 按钮色 80% + #0a2a6b，文字用 text-on-primary；深色与浅色主题各算一遍
const login = read("./login.css");
assert.ok(login.includes("linear-gradient(135deg, var(--brand-gradient), color-mix(in srgb, var(--brand-gradient) 80%, #0a2a6b))"),
  "登录按钮渐变写法变了，请同步这里的计算");
const themes = read("./tokens.css").split(':root[data-theme="light"]');
assert.equal(themes.length, 2, "tokens.css 的主题分段变了");
for (const [name, css] of [["深色", themes[0]], ["浅色", themes[1]]]) {
  const brand = rgb(css.match(/--brand-gradient:\s*(#[0-9a-f]{6})/i)[1]);
  const text = rgb(css.match(/--text-on-primary:\s*(#[0-9a-f]{6})/i)[1]);
  const end = mix(brand, rgb("#0a2a6b"), 0.8);
  assert.ok(worst(text, 1, brand, end) >= 4.5, `${name}主题登录按钮文字对比度不足 4.5:1`);
}
console.log("contrast: ok");
