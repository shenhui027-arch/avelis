// 语言包检查（ADR 0015）：node scripts/check-i18n.mjs（在 frontend/ 下）。任一项不通过时退出码为 1。
// 键是简体原文：源码里每个 t("原文") 就是一个键。检查：
// 1. 繁体与英文包覆盖源码里的全部键（带 context 的键是 原文_context；英文带 count 的键要有 _one 与 _other），没有空值；
// 2. 语言包里没有源码已不再使用的键（改了原文要同步改译文）；
// 3. 译文的插值参数与原文一致；
// 4. 英文包里没有中文，繁体包里没有常见简体字；
// 5. 业务中立（ADR 0010）：译文里不出现行业业务词（简体原文由后端 test_business_neutral.py 扫描源码覆盖）；
// 6. t() 的第一个参数必须是字面量，不能拼接或带插值的模板字符串（否则无法抽取）。
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const src = fileURLToPath(new URL("../src/", import.meta.url));
const dir = join(src, "i18n", "locales");
const packs = { "zh-TW": load("zh-TW"), en: load("en") };
const errors = [];

function load(lang) {
  return JSON.parse(readFileSync(join(dir, `${lang}.json`), "utf8"));
}

function walk(path, out = []) {
  for (const name of readdirSync(path)) {
    const p = join(path, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) out.push(p);
  }
  return out;
}

// 抽取源码里的键：t("…") / t('…') / t(`…`)，以及紧跟的 { context: "x" } 与 count
const used = new Map(); // key -> { plural, where }
const CALL = /\bt\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`)(?=\s*(,\s*\{([^}]*)\})?)/g; // 选项只向前看，不吞掉里面嵌套的 t()
for (const file of walk(src)) {
  const text = readFileSync(file, "utf8");
  const where = relative(src, file);
  for (const m of text.matchAll(CALL)) {
    const literal = m[1];
    if (literal.startsWith("`") && literal.includes("${")) {
      errors.push(`${where}: t() 的键不能是带插值的模板字符串：${literal.slice(0, 40)}`);
      continue;
    }
    const key = literal.startsWith("`") ? literal.slice(1, -1) : literal.startsWith("'")
      ? literal.slice(1, -1).replace(/\\'/g, "'") : JSON.parse(literal);
    const opts = m[3] ?? "";
    const context = opts.match(/context:\s*"([\w-]+)"/)?.[1];
    const full = context ? `${key}_${context}` : key;
    const prev = used.get(full);
    used.set(full, { plural: (prev?.plural ?? false) || /\bcount\b/.test(opts), where: prev?.where ?? where });
  }
  for (const m of text.matchAll(/\bt\(\s*[A-Za-z_$(]/g)) {
    const line = text.slice(0, m.index).split("\n").length;
    errors.push(`${where}:${line}: t() 的键必须是字符串字面量`);
  }
}

const params = (s) => [...new Set([...s.matchAll(/\{\{\s*([\w.]+)\s*(?:,[^}]*)?\}\}/g)].map((m) => m[1]))].sort().join(",");
const PLURAL = /_(zero|one|two|few|many|other)$/;

for (const [lang, pack] of Object.entries(packs)) {
  for (const [key, info] of used) {
    const forms = lang === "en" && info.plural ? [`${key}_one`, `${key}_other`] : [key];
    const ok = forms.every((f) => f in pack) || (key in pack && !(lang === "en" && info.plural && !(`${key}_one` in pack)));
    if (!ok) errors.push(`${lang}: 缺少译文「${key}」${lang === "en" && info.plural ? "（需 _one 与 _other）" : ""}（${info.where}）`);
  }
  for (const [key, value] of Object.entries(pack)) {
    const base = key.replace(PLURAL, "");
    if (!used.has(key) && !used.has(base)) errors.push(`${lang}: 多余的键「${key}」（源码里已没有）`);
    if (!String(value).trim()) errors.push(`${lang}: 空译文「${key}」`);
    const source = used.has(key) ? key : base;
    const want = params(source);
    if (params(String(value)) !== want) errors.push(`${lang}: 「${key}」的插值参数 [${params(String(value))}] 与原文 [${want}] 不一致`);
  }
}

const CJK = /[㐀-鿿]/;
for (const [key, value] of Object.entries(packs.en)) if (CJK.test(value)) errors.push(`en: 「${key}」的译文含中文：${value}`);
const SIMPLIFIED = /[们这对说时间现发经应关数据设页预览类项务认证录户该块问题错误码]/;
for (const [key, value] of Object.entries(packs["zh-TW"])) {
  const m = value.match(SIMPLIFIED);
  if (m) errors.push(`zh-TW: 「${key}」的译文含简体字“${m[0]}”：${value}`);
}

const BUSINESS = /成交额|成交額|门店|門店|門市|客单价|客單價|华东|華東|华南|華南|华北|華北|实付|實付|电商|電商|订单|訂單|\bstores?\b|\bshops?\b|\bGMV\b|\bsales\b|\brevenue\b|\be-?commerce\b|\bAOV\b|\bbasket\b|\bSKU\b/i;
for (const [lang, pack] of Object.entries(packs)) {
  for (const [key, value] of Object.entries(pack)) {
    const m = value.match(BUSINESS);
    if (m) errors.push(`${lang}: 「${key}」出现业务词“${m[0]}”（ADR 0010）：${value}`);
  }
}

// --todo <文件>：把缺译文的原文按源码顺序写成 [{ key, plural, where }]，方便补译
const todoAt = process.argv.indexOf("--todo");
if (todoAt > 0) {
  const todo = [...used].filter(([key]) => !(key in packs.en) && !(`${key}_one` in packs.en))
    .map(([key, info]) => ({ key, plural: info.plural, where: info.where }));
  writeFileSync(process.argv[todoAt + 1], JSON.stringify(todo, null, 1) + "\n");
  console.log(`i18n: ${todo.length} 条待译 → ${process.argv[todoAt + 1]}`);
}

if (errors.length) {
  console.error(`i18n: ${errors.length} 个问题\n` + errors.join("\n"));
  process.exit(1);
}
console.log(`i18n: ok（${used.size} 条原文 × 繁体 / 英文）`);
