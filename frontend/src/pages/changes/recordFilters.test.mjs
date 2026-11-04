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
// 当地零点不存在（智利 2026-09-06 00:00 直接跳到 01:00）：取当天第一个真实存在的时刻 01:00（-03:00）= 04:00Z
assert.equal(mod.zonedDayStart("2026-09-06", "America/Santiago"), "2026-09-06T04:00:00.000Z");
assert.equal(mod.zonedDayStart("2026-09-07", "America/Santiago"), "2026-09-07T03:00:00.000Z");
// 回拨日（智利 2026-04-05 00:00 -03 回拨成 04-04 23:00 -04）：04-05 第一次出现在 00:00 -04 = 04:00Z
assert.equal(mod.zonedDayStart("2026-04-05", "America/Santiago"), "2026-04-05T04:00:00.000Z");

const q = mod.recordQuery({ datasource_id: "d1", asset_id: "", column: "", kind: "column_added", actor: "", attribution_status: "",
  since: "2026-10-30", until: "2026-10-31" }, "Asia/Shanghai");
assert.deepEqual(q, { query: { datasource_id: "d1", kind: "column_added", since: "2026-10-29T16:00:00.000Z",
  until: "2026-10-30T16:00:00.000Z" }, invalid: [] });
// 整天不存在（萨摩亚 2011-12-30 跨日期变更线被跳过）：不抛异常，标为日期字段错误，不发出该条件
assert.throws(() => mod.zonedDayStart("2011-12-30", "Pacific/Apia"));
const skipped = mod.recordQuery({ datasource_id: "", asset_id: "", column: "", kind: "", actor: "", attribution_status: "",
  since: "2011-12-30", until: "2011-12-31" }, "Pacific/Apia");
assert.deepEqual(skipped.invalid, ["since"]);
assert.ok(!("since" in skipped.query) && skipped.query.until === "2011-12-30T10:00:00.000Z");

console.log("recordFilters: ok");
