// 由设计系统 Avelis UI 的 tokens.json 生成 CSS 变量（深色为默认主题，浅色用 [data-theme="light"]）。
import { readFileSync, writeFileSync } from "node:fs";

const tokens = JSON.parse(readFileSync(new URL("../design/tokens.json", import.meta.url), "utf8"));
const [primary, ...others] = tokens.color.themes.map((t) => t.id);
const value = (v, theme) => (typeof v === "object" ? v[theme] ?? v[primary] : v);
const lines = (theme) => {
  const out = [];
  for (const t of tokens.color.tokens) out.push(`  --${t.name}: ${value(t.value, theme)};`);
  for (const t of tokens.shadow?.tokens ?? []) out.push(`  --${t.name}: ${value(t.value, theme)};`);
  return out;
};
const base = [];
for (const [name, stack] of Object.entries(tokens.type.families)) base.push(`  --font-${name}: ${stack};`);
// 排版：每个样式生成 font 简写变量（--type-<name>），页面与组件用它而不是手写字号
for (const group of tokens.type.groups) {
  for (const st of group.styles) {
    base.push(`  --type-${st.name}: ${st.fontWeight} ${st.fontSize}/${st.lineHeight} var(--font-${group.family});`);
  }
}
for (const family of ["spacing", "radius", "size"]) {
  for (const t of tokens[family]?.tokens ?? []) base.push(`  --${t.name}: ${t.value};`);
}
const css = [
  `/* ${tokens.name} — generated from design/tokens.json by scripts/gen-tokens.mjs; do not edit */`,
  `:root {\n  color-scheme: dark;\n${[...base, ...lines(primary)].join("\n")}\n}`,
  ...others.map((id) => `:root[data-theme="${id}"] {\n  color-scheme: ${id};\n${lines(id).join("\n")}\n}`),
  "",
].join("\n\n");
writeFileSync(new URL("../src/styles/tokens.css", import.meta.url), css);
console.log("tokens.css written");
