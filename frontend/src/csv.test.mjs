// Run: node src/csv.test.mjs (from frontend/).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./csv.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const m = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

assert.equal(m.csvCell("abc"), '"abc"');
assert.equal(m.csvCell('a"b'), '"a""b"');
assert.equal(m.csvCell(null), '""');
assert.equal(m.csvCell(undefined), '""');
assert.equal(m.csvCell(12.5), '"12.5"');
for (const risky of ["=SUM(A1)", "+1", "-1", "@x", "\tx", "\rx"]) assert.equal(m.csvCell(risky), `"'${risky}"`);
assert.equal(m.toCsv([["a", "b"], [1, null]]), '﻿"a","b"\r\n"1",""');
assert.equal(m.toCsv([]), "﻿");

console.log("csv: all assertions passed");
