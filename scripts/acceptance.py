"""一条命令复现 Avelis v0.1 的验收：环境 → 基线 → 后端单元测试 → 验收（故事 1–9 与异常场景）→ 前端构建。

在仓库根目录运行：
  uv run scripts/acceptance.py            在现有演示容器上复现（容器未启动时会启动）
  uv run scripts/acceptance.py --fresh    从零开始：删除演示容器与数据卷后重建（StarRocks 首次启动约需数分钟）
  uv run scripts/acceptance.py --no-llm   跳过调用真实模型的用例（不向模型服务发送任何数据）
  uv run scripts/acceptance.py --no-engines  不启动更多引擎的演示库（ClickHouse、Doris），跳过它们的验收
  uv run scripts/acceptance.py --no-recreate 容器已存在时不重建（从 git worktree 等其他检出目录运行时必须加：
                                             演示容器由所有检出目录共用，挂载路径不同会让 Docker 重建它们）

前置条件：Docker（Compose v2）、uv、pnpm（--skip-frontend 时可不装）。口令与密钥在 .env 中自动生成，不打印。
.env 中配置了 AVELIS_MODEL_API_KEY 时，默认会运行 llm 用例：问题与指标元数据会发送到配置的模型服务。
结束后演示环境回到故事 1 的起点（用户、业务域、数据源与首次采集已就绪），可直接打开界面走查。
报告写入 .demo/acceptance-report.md 与 .json。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import demo

ROOT = demo.ROOT
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
REPORT_DIR = demo.STATE / "acceptance"


class StepFailed(Exception):
    pass


class Runner:
    def __init__(self) -> None:
        self.steps: list[dict] = []

    def step(self, name: str, fn) -> object:
        demo.log(f"\n== {name} ==")
        started = time.monotonic()
        record = {"name": name, "status": "failed", "seconds": 0.0, "detail": None}
        self.steps.append(record)
        try:
            result = fn()
            record["status"] = "passed"
            return result
        except StepFailed as exc:
            record["detail"] = str(exc)
            raise
        except subprocess.CalledProcessError as exc:
            record["detail"] = f"命令失败（退出码 {exc.returncode}）：{' '.join(map(str, exc.cmd))}"
            raise StepFailed(record["detail"]) from exc
        finally:
            record["seconds"] = round(time.monotonic() - started, 1)


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    demo.log("$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True, env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})})


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise StepFailed(f"未找到 {name}，请先安装")
    return path


def junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    cases = []
    for case in suite.iter("testcase"):
        outcome = "passed"
        message = None
        for tag in ("failure", "error", "skipped"):
            node = case.find(tag)
            if node is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = (node.get("message") or "")[:300]
        cases.append({"name": f"{case.get('classname', '').split('.')[-1]}::{case.get('name')}", "outcome": outcome,
                      "seconds": round(float(case.get("time", 0)), 1), "message": message})
    return {"total": len(cases), **{k: sum(c["outcome"] == k for c in cases)
                                    for k in ("passed", "failed", "error", "skipped")}, "cases": cases}


def pytest_step(marker: str | None, name: str, extra_env: dict[str, str] | None = None) -> dict:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    xml = REPORT_DIR / f"{name}.xml"
    cmd = [tool("uv"), "run", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}"]
    if marker:
        cmd += ["-m", marker]
    try:
        run(cmd, BACKEND, extra_env)
    finally:
        summary = junit(xml) if xml.exists() else None
    if summary is None or summary["failed"] or summary["error"]:
        raise StepFailed(f"{name}：{summary and summary['failed'] + summary['error']} 项未通过")
    return summary


def write_report(runner: Runner, info: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (demo.STATE / "acceptance-report.json").write_text(
        json.dumps({**info, "steps": runner.steps}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# Avelis 验收报告", "", f"- 时间：{info['finished_at']}（UTC）", f"- 模式：{info['mode']}",
             f"- 结论：**{'全部通过' if info['ok'] else '未通过'}**",
             f"- 更多引擎：{info.get('engines', '—')}；真实模型用例：{info.get('llm', '—')}", "", "## 步骤", "",
             "| 步骤 | 结果 | 用时（秒） | 说明 |", "|---|---|---|---|"]
    for s in runner.steps:
        lines.append(f"| {s['name']} | {'通过' if s['status'] == 'passed' else '失败'} | {s['seconds']} | "
                     f"{(s['detail'] or '').replace('|', '/')} |")
    if info.get("baseline"):
        lines += ["", "## 演示库基线（只读账号查询）", "", "| 项 | 期望 | 实际 | 结果 |", "|---|---|---|---|"]
        lines += [f"| {b['label']} | {b['expected']} | {b['actual']} | {'通过' if b['passed'] else '不符'} |"
                  for b in info["baseline"]]
    titles = (("unit", "后端单元测试"),
              ("acceptance", "验收（真实 StarRocks / MySQL / PostgreSQL / ClickHouse / Doris / Valkey）"))
    for key, title in titles:
        summary = info.get(key)
        if not summary:
            continue
        lines += ["", f"## {title}：{summary['passed']} / {summary['total']} 通过"
                  + (f"，跳过 {summary['skipped']}" if summary["skipped"] else ""), ""]
        if key == "acceptance":
            lines += ["| 用例 | 结果 | 用时（秒） |", "|---|---|---|"]
            lines += [f"| {c['name']} | {c['outcome']} | {c['seconds']} |" for c in summary["cases"]]
    out = demo.STATE / "acceptance-report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fresh", action="store_true", help="删除演示容器与数据卷后从零重建")
    parser.add_argument("--no-llm", action="store_true", help="跳过调用真实模型的用例")
    parser.add_argument("--skip-frontend", action="store_true", help="不安装依赖、不构建前端")
    parser.add_argument("--no-engines", action="store_true", help="不启动更多引擎的演示库，跳过它们的验收")
    parser.add_argument("--no-recreate", action="store_true",
                        help="容器已存在时不重建：从 worktree 等其他检出目录运行时使用（演示容器由多个检出目录共用）")
    args = parser.parse_args()

    runner = Runner()
    info: dict = {"mode": "从零重建" if args.fresh else "复用现有容器",
                  "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        tools = ["docker", "uv"] + ([] if args.skip_frontend else ["pnpm"])
        runner.step("检查前置工具", lambda: [tool(t) for t in tools])
        if args.fresh:
            def teardown() -> None:
                if demo.ENV_FILE.exists():
                    demo.down(volumes=True)
            runner.step("删除演示容器与数据卷", teardown)
        engines = not args.no_engines
        info["engines"] = "运行（ClickHouse、Doris 真实实例）" if engines else "跳过"
        runner.step("启动演示环境并灌入基线数据",
                    lambda: demo.up(skip_seed=False, engines=engines, no_recreate=args.no_recreate))

        def baseline() -> list[dict]:
            env = demo.load_env()
            values = demo.baseline_values(env) + (demo.engine_baseline_values(env) if engines else [])
            info["baseline"] = values
            if not all(v["passed"] for v in values):
                raise StepFailed("演示库基线不符")
            return values
        runner.step("核对演示库基线", baseline)
        runner.step("安装后端依赖（锁文件）", lambda: run([tool("uv"), "sync", "--frozen"], BACKEND))
        runner.step("迁移元数据库", lambda: run([tool("uv"), "run", "python", "-m", "avelis.bootstrap", "migrate"],
                                                BACKEND))
        info["unit"] = runner.step("后端单元测试", lambda: pytest_step(None, "unit"))
        has_key = bool(demo.load_env().get("AVELIS_MODEL_API_KEY"))
        llm = has_key and not args.no_llm
        info["llm"] = "运行（会向配置的模型服务发送问题与指标元数据）" if llm else "跳过"
        marker = "acceptance" + ("" if llm else " and not llm") + ("" if engines else " and not engines")
        info["acceptance"] = runner.step("验收：故事 1–9 与异常场景", lambda: pytest_step(marker, "acceptance"))
        if not args.skip_frontend:
            runner.step("安装前端依赖（锁文件）", lambda: run([tool("pnpm"), "install", "--frozen-lockfile"], FRONTEND))
            runner.step("前端类型检查与构建", lambda: (run([tool("pnpm"), "exec", "tsc", "-b"], FRONTEND),
                                                   run([tool("pnpm"), "build"], FRONTEND)))
        runner.step("演示环境回到故事 1 起点", lambda: run(
            [tool("uv"), "run", "python", "-m", "avelis.bootstrap", "demo", "--reset"], BACKEND))
        info["ok"] = True
    except StepFailed as exc:
        info["ok"] = False
        demo.log(f"\n失败：{exc}")
    info["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report = write_report(runner, info)
    demo.log(f"\n验收{'全部通过' if info['ok'] else '未通过'}；报告：{report.relative_to(ROOT)}")
    for s in runner.steps:
        demo.log(f"  {'通过' if s['status'] == 'passed' else '失败'}  {s['name']}（{s['seconds']} 秒）")
    sys.exit(0 if info["ok"] else 1)


if __name__ == "__main__":
    main()
