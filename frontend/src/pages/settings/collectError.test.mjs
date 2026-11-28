// Run: node src/pages/settings/collectError.test.mjs (from frontend/).
// 采集错误显示：有说明时显示说明并附原文，认不出时显示原文，都没有时用兜底文字。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./collectError.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { errorParts, shortRaw, bannerText, RAW_LIMIT } = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

const RAW = "OperationalError: (2003, \"Can't connect to MySQL server\")";

assert.deepEqual(errorParts({ error: RAW, error_message: "无法连接到数据库" }, "采集失败"), { text: "无法连接到数据库", raw: RAW });
assert.deepEqual(errorParts({ error: `  ${RAW}\n`, error_message: " 无法连接到数据库 " }, "x"), { text: "无法连接到数据库", raw: RAW });
assert.deepEqual(errorParts({ error: RAW, error_message: null }, "采集失败"), { text: RAW, raw: null });
assert.deepEqual(errorParts({ error: RAW, error_message: "   " }, "采集失败"), { text: RAW, raw: null });
assert.deepEqual(errorParts({ error: null, error_message: "连接超时" }, "采集失败"), { text: "连接超时", raw: null });
assert.deepEqual(errorParts({ error: "连接超时", error_message: "连接超时" }, "x"), { text: "连接超时", raw: null });
assert.deepEqual(errorParts({ error: "", error_message: null }, "连接测试失败"), { text: "连接测试失败", raw: null });
assert.deepEqual(errorParts(null, "采集失败"), { text: "采集失败", raw: null });
assert.deepEqual(errorParts(undefined, "采集失败"), { text: "采集失败", raw: null });

assert.equal(RAW_LIMIT, 160);
assert.equal(shortRaw("abc"), "abc");
assert.equal(shortRaw("a".repeat(160)), "a".repeat(160));
assert.equal(shortRaw("a".repeat(161)), `${"a".repeat(160)}…`);
assert.equal(shortRaw(`${"a".repeat(158)}   bbb`), `${"a".repeat(158)}…`);

const zh = (text, raw) => `${text}（原文：${raw}）`;
const en = (text, raw) => `${text} (raw: ${raw})`;
assert.equal(bannerText({ error: RAW, error_message: "无法连接到数据库" }, "采集失败", zh), `无法连接到数据库（原文：${RAW}）`);
assert.equal(bannerText({ error: RAW, error_message: "Unable to connect" }, "f", en), `Unable to connect (raw: ${RAW})`);
assert.equal(bannerText({ error: RAW }, "采集失败", zh), RAW);
assert.equal(bannerText({}, "采集失败", zh), "采集失败");
assert.equal(bannerText({ error: "x".repeat(200), error_message: "权限不足" }, "f", zh), `权限不足（原文：${"x".repeat(160)}…）`);

console.log("collectError: ok");
