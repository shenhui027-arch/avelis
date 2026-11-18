// Run: node src/pages/settings/contextWindow.test.mjs (from frontend/).
// 上下文窗口支持 k / M 单位：解析、范围与显示往返一致。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./contextWindow.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { parseContextWindow: parse, formatContextWindow: format } = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

assert.equal(parse(""), null);
assert.equal(parse("  "), null);
assert.equal(parse("1M"), 1_000_000);
assert.equal(parse("1m"), 1_000_000);
assert.equal(parse("1.5M"), 1_500_000);
assert.equal(parse("200k"), 200_000);
assert.equal(parse("200 K"), 200_000);
assert.equal(parse("128000"), 128_000);
assert.equal(parse("128,000"), 128_000);
assert.equal(parse("131072"), 131_072);
assert.equal(parse("1k"), undefined, "低于下限");
assert.equal(parse("11M"), undefined, "高于上限");
assert.equal(parse("abc"), undefined);
assert.equal(parse("1G"), undefined);
assert.equal(parse("-5k"), undefined);

assert.equal(format(null), "");
assert.equal(format(1_000_000), "1M");
assert.equal(format(1_500_000), "1.5M");
assert.equal(format(200_000), "200k");
assert.equal(format(128_000), "128k");
assert.equal(format(131_072), "131072");
for (const n of [1024, 4096, 128_000, 131_072, 200_000, 1_000_000, 1_500_000, 2_000_000, 10_000_000]) assert.equal(parse(format(n)), n, String(n));

console.log("contextWindow: ok");
