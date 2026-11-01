// 前端测试：node scripts/run-tests.mjs（在 frontend/ 下，即 pnpm test）。
// 逐个运行 src 下的 *.test.mjs（每个测试自带断言与输出），任一失败时退出码为 1。
import { spawnSync } from "node:child_process";
import { readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));

function walk(path, out = []) {
  for (const name of readdirSync(path)) {
    const p = join(path, name);
    if (statSync(p).isDirectory()) walk(p, out);
    else if (name.endsWith(".test.mjs")) out.push(p);
  }
  return out;
}

const tests = walk(join(root, "src")).sort();
let failed = 0;
for (const file of tests) {
  const name = relative(root, file);
  const run = spawnSync(process.execPath, [file], { cwd: root, encoding: "utf8" });
  const output = `${run.stdout ?? ""}${run.stderr ?? ""}`.trim();
  if (run.status === 0) {
    console.log(`ok   ${name}${output ? ` — ${output.split("\n").at(-1)}` : ""}`);
  } else {
    failed += 1;
    console.log(`FAIL ${name}\n${output}`);
  }
}
console.log(`${tests.length - failed}/${tests.length} 通过`);
process.exit(failed ? 1 : 0);
