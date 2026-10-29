// Run: node src/pages/changes/recordFilters.test.mjs (from frontend/).
// 日期筛选按业务时区解释；选择数据源后资产下拉按 datasource.id 联动（/api/assets 的真实结构）。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("./recordFilters.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText;
const mod = await import("data:text/javascript;base64," + Buffer.from(js).toString("base64"));

// Asia/Shanghai 当天 00:00 = 前一天 16:00Z，与浏览器时区无关
assert.equal(mod.zonedDayStart("2026-10-30", "Asia/Shanghai"), "2026-10-29T16:00:00.000Z");
assert.equal(mod.zonedDayStart("2026-10-30", "UTC"), "2026-10-30T00:00:00.000Z");
// 夏令时切换日（纽约 2026-03-08 凌晨 2 点切换，零点仍是 -05:00）
assert.equal(mod.zonedDayStart("2026-03-08", "America/New_York"), "2026-03-08T05:00:00.000Z");
assert.equal(mod.zonedDayStart("2026-03-09", "America/New_York"), "2026-03-09T04:00:00.000Z");

const q = mod.recordQuery({ datasource_id: "d1", asset_id: "", column: "", kind: "column_added", actor: "", attribution_status: "",
  since: "2026-10-30", until: "2026-10-31" }, "Asia/Shanghai");
assert.deepEqual(q, { datasource_id: "d1", kind: "column_added", since: "2026-10-29T16:00:00.000Z", until: "2026-10-30T16:00:00.000Z" });

const assets = [{ id: "a1", datasource: { id: "d1" } }, { id: "a2", datasource: { id: "d2" } }];
assert.deepEqual(mod.assetsOfDatasource(assets, "d1").map((a) => a.id), ["a1"]);
assert.deepEqual(mod.assetsOfDatasource(assets, "").map((a) => a.id), ["a1", "a2"]);
console.log("recordFilters: ok");
