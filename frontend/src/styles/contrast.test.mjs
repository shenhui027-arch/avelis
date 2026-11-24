// Run: node src/styles/contrast.test.mjs (from frontend/).
// issue 132 & M2A.1：蓝底上的小字与全量浅/深色语义令牌均需满足 WCAG 2.1 AA（正文 ≥4.5:1，非文本 UI/图形/焦点环 ≥3.0:1）。
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

/** Parse CSS custom properties (--name: val;) from a theme block in tokens.css. */
const parseVars = (css) => {
  const out = {};
  for (const m of css.matchAll(/--([a-z0-9-]+):\s*([^;]+);/gi)) {
    out[m[1]] = m[2].trim();
  }
  return out;
};

/** Parse hex (#rrggbb / #rrggbbaa) or rgba(r, g, b, a) into { rgb, alpha }. */
const parseColor = (val) => {
  if (/^#[0-9a-f]{6}$/i.test(val)) return { rgb: rgb(val), alpha: 1 };
  if (/^#[0-9a-f]{8}$/i.test(val)) {
    return { rgb: rgb(val.slice(0, 7)), alpha: parseInt(val.slice(7, 9), 16) / 255 };
  }
  const m = val.match(/^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)$/i);
  if (m) return { rgb: [Number(m[1]), Number(m[2]), Number(m[3])], alpha: m[4] == null ? 1 : Number(m[4]) };
  throw new Error(`Unsupported color token value: ${val}`);
};

/** Composite a potentially translucent color over an opaque RGB background. */
const over = (fgVal, bgRgb) => {
  const c = parseColor(fgVal);
  return c.alpha >= 1 ? c.rgb : mix(c.rgb, bgRgb, c.alpha);
};

const assertAA = (themeName, label, fgRgb, bgRgb, minRatio = 4.5) => {
  const ratio = contrast(fgRgb, bgRgb);
  assert.ok(ratio >= minRatio, `${themeName}主题 ${label} 对比度 ${ratio.toFixed(2)}:1 < ${minRatio}:1`);
};

const kit = read("./kit.css");
// 渐变两端是令牌（深浅两套同值），从 tokens.css 读
const tokens = read("./tokens.css");
const from = rgb(tokens.match(/--hero-from:\s*(#[0-9a-f]{6})/i)[1]);
const to = rgb(tokens.match(/--hero-to:\s*(#[0-9a-f]{6})/i)[1]);
assert.ok(kit.includes("var(--hero-from)") && kit.includes("var(--hero-to)"), "采集卡片不再使用 hero 渐变令牌，请同步这里的计算");
const heroRules = kit.slice(kit.indexOf(".av-hero-card {"), kit.indexOf(".av-hero-card .av-heat {"));
const alphas = [...heroRules.matchAll(/color: rgba\(255, 255, 255, ([.\d]+)\)/g)].map((m) => Number(m[1]));
const axis = Number(kit.match(/\.av-hero-card \.av-cadence-axis \{[^}]*rgba\(255, 255, 255, ([.\d]+)\)/)[1]);
assert.ok(alphas.length >= 3, "采集卡片的小字颜色没读到");
for (const alpha of [...alphas, axis, 1]) {
  assert.ok(worst(WHITE, alpha, from, to) >= 4.5, `蓝底卡片上 ${alpha} 透明度的白字对比度不足 4.5:1`);
}

// .av-hero-card__hd .av-tag：深蓝半透明玻璃胶囊底色上的白字对比度（全渐变 >= 4.5:1）
const tagMatch = kit.match(/\.av-hero-card__hd\s+\.av-tag\s*\{[^}]*background:\s*rgba\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([.\d]+)\s*\)/);
assert.ok(tagMatch, "未读到 .av-hero-card__hd .av-tag 的 rgba 背景色");
const tagBgRgb = [Number(tagMatch[1]), Number(tagMatch[2]), Number(tagMatch[3])];
const tagBgAlpha = Number(tagMatch[4]);
for (const tPos of [0, 0.25, 0.5, 0.75, 1]) {
  const heroBg = mix(to, from, tPos);
  const pillBg = mix(tagBgRgb, heroBg, tagBgAlpha);
  assertAA("全站", `.av-hero-card__hd .av-tag (t=${tPos})`, WHITE, pillBg, 4.5);
}

// .av-hero-card .av-heat > i.is-change：外圈暗色隔离环 + 琥珀高亮环（均需与内外底色 >= 3.0:1）
const heatChangeMatch = kit.match(/\.av-hero-card\s+\.av-heat\s*>\s*i\.is-change\s*\{[^}]*box-shadow:\s*0\s+0\s+0\s+[\d.]+px\s+rgba\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([.\d]+)\s*\)\s*,\s*0\s+0\s+0\s+[\d.]+px\s+(#[0-9a-f]{6})/i);
assert.ok(heatChangeMatch, "未读到 .av-hero-card .av-heat > i.is-change 的双层外环阴影");
const sepRgb = [Number(heatChangeMatch[1]), Number(heatChangeMatch[2]), Number(heatChangeMatch[3])];
const sepAlpha = Number(heatChangeMatch[4]);
const ringRgb = rgb(heatChangeMatch[5]);
for (const tPos of [0, 0.25, 0.5, 0.75, 1]) {
  const heroBg = mix(to, from, tPos);
  const sepBg = mix(sepRgb, heroBg, sepAlpha);
  assertAA("全站", `.av-hero-card .av-heat > i.is-change 外环 vs 卡片底 (t=${tPos})`, ringRgb, heroBg, 3.0);
  assertAA("全站", `.av-hero-card .av-heat > i.is-change 外环 vs 暗隔离环 (t=${tPos})`, ringRgb, sepBg, 3.0);
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
const themes = tokens.split(':root[data-theme="light"]');
assert.equal(themes.length, 2, "tokens.css 的主题分段变了");
for (const [name, css] of [["深色", themes[0]], ["浅色", themes[1]]]) {
  const brand = rgb(css.match(/--brand-gradient:\s*(#[0-9a-f]{6})/i)[1]);
  const text = rgb(css.match(/--text-on-primary:\s*(#[0-9a-f]{6})/i)[1]);
  const end = mix(brand, rgb("#0a2a6b"), 0.8);
  assert.ok(worst(text, 1, brand, end) >= 4.5, `${name}主题登录按钮文字对比度不足 4.5:1`);
}

// M2A.1 全量浅/深色语义令牌 WCAG AA 校验矩阵
const SURFACES = ["bg-canvas", "bg-base", "bg-surface", "bg-raised", "bg-overlay", "bg-active", "bg-seg-thumb"];
const TEXT_FGS = [
  "text-primary", "text-secondary", "text-tertiary",
  "accent-text", "accent", "signal", "flow", "ok", "warn", "danger",
  "mod-collect-text", "mod-ask-text", "mod-lineage-text", "mod-catalog-text", "mod-change-text",
];
const UI_FGS = [
  "focus-ring",
  "mod-collect", "mod-ask", "mod-lineage", "mod-catalog", "mod-change",
  "domain-violet", "domain-teal", "domain-sky", "domain-rose", "domain-lime", "domain-sand",
];
const SUBTLE_COMPOSITES = ["bg-canvas", "bg-base", "bg-surface", "bg-raised", "bg-overlay", "bg-active"];
const SUBTLE_PAIRS = [
  { bg: "accent-subtle", text: ["accent-text", "accent"], ui: [] },
  { bg: "signal-subtle", text: ["signal", "flow"], ui: [] },
  { bg: "ok-subtle", text: ["ok"], ui: [] },
  { bg: "warn-subtle", text: ["warn"], ui: [] },
  { bg: "danger-subtle", text: ["danger"], ui: [] },
  { bg: "mod-collect-subtle", text: ["mod-collect-text"], ui: ["mod-collect"] },
  { bg: "mod-ask-subtle", text: ["mod-ask-text"], ui: ["mod-ask"] },
  { bg: "mod-lineage-subtle", text: ["mod-lineage-text"], ui: ["mod-lineage"] },
  { bg: "mod-catalog-subtle", text: ["mod-catalog-text"], ui: ["mod-catalog"] },
  { bg: "mod-change-subtle", text: ["mod-change-text"], ui: ["mod-change"] },
];
const VIZ_SERIES = ["viz-1", "viz-2", "viz-3", "viz-4", "viz-5", "viz-6", "viz-7", "viz-8", "viz-neutral"];

for (const [name, css, isLight] of [["深色", themes[0], false], ["浅色", themes[1], true]]) {
  const v = parseVars(css);

  // 1. Base surfaces x Text FGs (>= 4.5:1) and UI/Icon/Focus/Domain FGs (>= 3.0:1)
  for (const sName of SURFACES) {
    const sRgb = parseColor(v[sName]).rgb;
    for (const fg of TEXT_FGS) {
      assertAA(name, `${fg} on ${sName}`, parseColor(v[fg]).rgb, sRgb, 4.5);
    }
    for (const fg of UI_FGS) {
      assertAA(name, `${fg} on ${sName}`, parseColor(v[fg]).rgb, sRgb, 3.0);
    }
  }

  // 2. Subtle tints composited over base surfaces
  for (const pair of SUBTLE_PAIRS) {
    for (const baseName of SUBTLE_COMPOSITES) {
      const compBg = over(v[pair.bg], parseColor(v[baseName]).rgb);
      for (const fg of ["text-primary", "text-secondary", "text-tertiary", ...pair.text]) {
        assertAA(name, `${fg} on ${pair.bg}@${baseName}`, parseColor(v[fg]).rgb, compBg, 4.5);
      }
      for (const fg of pair.ui) {
        assertAA(name, `${fg} on ${pair.bg}@${baseName}`, parseColor(v[fg]).rgb, compBg, 3.0);
      }
    }
  }

  // 3. Solid controls, hover states & login disabled states
  const onPrimary = parseColor(v["text-on-primary"]).rgb;
  const onAccent = parseColor(v["text-on-accent"]).rgb;
  const brand = parseColor(v["brand-gradient"]).rgb;
  const bgBase = parseColor(v["bg-base"]).rgb;
  assertAA(name, "text-on-primary on brand-gradient", onPrimary, brand, 4.5);
  assertAA(name, "text-on-primary on accent", onPrimary, parseColor(v["accent"]).rgb, 4.5);
  assertAA(name, "text-on-primary on accent-hover", onPrimary, parseColor(v["accent-hover"]).rgb, 4.5);
  assertAA(name, "text-on-primary on .av-btn--primary:hover", onPrimary, mix(brand, bgBase, 0.86), 4.5);
  assertAA(name, "text-on-accent on danger-solid", onAccent, parseColor(v["danger-solid"]).rgb, 4.5);
  assertAA(name, "text-on-accent on danger-solid-hover", onAccent, parseColor(v["danger-solid-hover"]).rgb, 4.5);
  assertAA(name, "text-on-accent on accent (.av-switch dot)", onAccent, parseColor(v["accent"]).rgb, 3.0);

  const disabledBg = isLight
    ? mix(brand, parseColor(v["bg-raised"]).rgb, 0.10)
    : mix(brand, parseColor(v["bg-surface"]).rgb, 0.22);
  assertAA(name, "text-secondary on .av-login__submit:disabled", parseColor(v["text-secondary"]).rgb, disabledBg, 4.5);

  // 4. Chart series on bg-surface (>= 3.0:1)
  const chartBg = parseColor(v["bg-surface"]).rgb;
  for (const viz of VIZ_SERIES) {
    assertAA(name, `${viz} on bg-surface`, parseColor(v[viz]).rgb, chartBg, 3.0);
  }
}

console.log("contrast: ok");
