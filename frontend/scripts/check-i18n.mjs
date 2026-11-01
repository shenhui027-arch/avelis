// 语言包检查（ADR 0015）：node scripts/check-i18n.mjs（在 frontend/ 下）。任一项不通过时退出码为 1。
// 1. 三种语言的键一致（复数后缀 _one/_other 归到同一个键），没有空值；
// 2. 同一个键在三种语言里的插值参数一致；
// 3. 英文包里没有残留中文，繁体包里没有常见简体字；
// 4. 业务中立（ADR 0010）：语言包里不出现行业业务词（简体词表在后端 test_business_neutral.py，这里补繁体与英文）；
// 5. 源码里用到的静态键都在简体包里（tsc 也会查，这里兜住 tx() 等动态入口之外的字符串键）。
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const root = new URL("../src/", import.meta.url);
const dir = new URL("i18n/locales/", root);
const LANGS = ["zh-CN", "zh-TW", "en"];
const PLURAL = /_(zero|one|two|few|many|other)$/;

const errors = [];
const packs = Object.fromEntries(LANGS.map((l) => [l, JSON.parse(readFileSync(new URL(`${l}.json`, dir), "utf8"))]));

function flatten(obj, prefix = "", out = new Map()) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object") flatten(v, key, out);
    else out.set(key, String(v));
  }
  return out;
}

const params = (s) => [...s.matchAll(/\{\{\s*([\w.]+)\s*(?:,[^}]*)?\}\}/g)].map((m) => m[1]).filter((p) => p !== "count").sort().join(",");

// 按“基础键”归组：{ base -> [值…] }
const grouped = Object.fromEntries(LANGS.map((l) => {
  const g = new Map();
  for (const [key, value] of flatten(packs[l])) {
    const base = key.replace(PLURAL, "");
    if (!value.trim()) errors.push(`${l}: 空值 ${key}`);
    g.set(base, [...(g.get(base) ?? []), value]);
  }
  return [l, g];
}));

const reference = grouped["zh-CN"];
for (const l of LANGS.slice(1)) {
  for (const base of reference.keys()) if (!grouped[l].has(base)) errors.push(`${l}: 缺少键 ${base}`);
  for (const base of grouped[l].keys()) if (!reference.has(base)) errors.push(`${l}: 多出键 ${base}（简体包里没有）`);
  for (const [base, values] of grouped[l]) {
    const ref = reference.get(base);
    if (!ref) continue;
    const want = params(ref[0]);
    for (const v of values) if (params(v) !== want) errors.push(`${l}: ${base} 的插值参数 [${params(v)}] 与简体 [${want}] 不一致`);
  }
}

// 英文包不应有中文；繁体包不应有常见简体字（转换遗漏）
const CJK = /[㐀-鿿]/;
for (const [key, value] of flatten(packs.en)) if (CJK.test(value)) errors.push(`en: ${key} 含中文：${value}`);
const SIMPLIFIED = /[们这对说时间现发经应关数据设页预览类项务认证录户该块问题错误码]/;
for (const [key, value] of flatten(packs["zh-TW"])) {
  const m = value.match(SIMPLIFIED);
  if (m) errors.push(`zh-TW: ${key} 含简体字“${m[0]}”：${value}`);
}

// 业务中立：繁体与英文的行业词（简体词由后端扫描覆盖，这里也一并查）
const BUSINESS = /成交额|成交額|门店|門店|門市|客单价|客單價|华东|華東|华南|華南|华北|華北|实付|實付|电商|電商|订单|訂單|\bstores?\b|\bshops?\b|\bGMV\b|\bsales\b|\brevenue\b|\be-?commerce\b|\bAOV\b|\bbasket\b|\bSKU\b/i;
for (const l of LANGS) {
  for (const [key, value] of flatten(packs[l])) {
    const m = value.match(BUSINESS);
    if (m) errors.push(`${l}: ${key} 出现业务词“${m[0]}”（ADR 0010）：${value}`);
  }
}

// 源码里的静态键 t("a.b") / t(`a.b`) 必须存在
function walk(path, out = []) {
  for (const name of readdirSync(path)) {
    const p = join(path, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(ts|tsx)$/.test(name) && !name.endsWith(".d.ts")) out.push(p);
  }
  return out;
}
const flatRef = new Set([...flatten(packs["zh-CN"]).keys()].map((k) => k.replace(PLURAL, "")));
for (const file of walk(new URL(".", root).pathname.replace(/^\/([A-Za-z]:)/, "$1"))) {
  const text = readFileSync(file, "utf8");
  for (const m of text.matchAll(/\bt\(\s*["'`]([\w.-]+)["'`]/g)) {
    if (!flatRef.has(m[1])) errors.push(`${file}: 未定义的键 ${m[1]}`);
  }
}

const total = reference.size;
if (errors.length) {
  console.error(`i18n: ${errors.length} 个问题\n` + errors.join("\n"));
  process.exit(1);
}
console.log(`i18n: ok（${total} 个键 × ${LANGS.length} 种语言）`);
