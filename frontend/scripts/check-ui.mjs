// 界面控件检查：node scripts/check-ui.mjs（在 frontend/ 下）。任一项不通过时退出码为 1。
// 浏览器原生的弹出层由系统绘制，在深色玻璃界面里会变成白底或系统风格，所以源码里不允许出现：
// - <select> / <datalist> / input list=…：下拉用 Select，带候选的输入用 SuggestInput；
// - type="date|time|datetime-local|month|week"：日期用 DateInput，时间用两个 Select；
// - confirm() / alert() / prompt()：二次确认用 ConfirmDialog，提示用 Banner 或 toast。
// 组件都在 src/components/controls.tsx，规则见 docs/development.md 的“界面控件”。
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const src = fileURLToPath(new URL("../src/", import.meta.url));

const RULES = [
  [/<select\b/, "原生 <select>：改用 components/ui 的 Select"],
  [/<datalist\b|<input\b[^>]*\blist=/, "原生 datalist：改用 SuggestInput"],
  [/type=["'{`]*(date|time|datetime-local|month|week)["'`}]/, "原生日期/时间输入：改用 DateInput（时间用两个 Select）"],
  [/(?<![\w.$])(window\.)?(confirm|alert|prompt)\(/, "浏览器原生对话框：改用 ConfirmDialog / Banner"],
];

function walk(path, out = []) {
  for (const name of readdirSync(path)) {
    const p = join(path, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (/\.(tsx|ts)$/.test(name) && !name.endsWith(".d.ts")) out.push(p);
  }
  return out;
}

const errors = [];
for (const file of walk(src)) {
  readFileSync(file, "utf8").split(/\r?\n/).forEach((line, i) => {
    if (/^\s*(\/\/|\*|\/\*)/.test(line)) return; // 注释里提到原生控件不算
    for (const [pattern, message] of RULES) {
      if (pattern.test(line)) errors.push(`${relative(src, file)}:${i + 1}  ${message}`);
    }
  });
}

if (errors.length) {
  console.error(`界面控件：${errors.length} 处使用了浏览器原生弹出层`);
  for (const e of errors) console.error(`  ${e}`);
  process.exit(1);
}
console.log("界面控件：ok（无原生下拉、日期、datalist 与原生对话框）");
