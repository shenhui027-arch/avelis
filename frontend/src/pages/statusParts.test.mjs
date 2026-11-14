// Run: node src/pages/statusParts.test.mjs (from frontend/).
// issue 129：已驳回与归档也计入，未知状态归入“其他”，分段之和等于指标总数。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const ts = createRequire(import.meta.url)("typescript");
const source = readFileSync(new URL("./statusParts.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const { statusParts } = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

const parts = (statuses) => statusParts(statuses, (s) => s, "other").filter((p) => p.value > 0);
const total = (statuses) => parts(statuses).reduce((sum, p) => sum + p.value, 0);

assert.deepEqual(parts(["published", "rejected"]).map((p) => [p.label, p.value]), [["published", 1], ["rejected", 1]]);
assert.deepEqual(parts(["published", "archived"]).map((p) => [p.label, p.value]), [["published", 1], ["archived", 1]]);
assert.deepEqual(parts(["rejected"]).map((p) => p.label), ["rejected"]);
assert.deepEqual(parts(["archived"]).map((p) => p.label), ["archived"]);
assert.deepEqual(parts(["something_new", "published"]).map((p) => [p.label, p.value]), [["published", 1], ["other", 1]]);
for (const statuses of [["published", "pending_review", "affected", "draft", "retired", "rejected", "archived"], ["x", "y", "draft"], []]) {
  assert.equal(total(statuses), statuses.length);
}
console.log("statusParts: ok");
