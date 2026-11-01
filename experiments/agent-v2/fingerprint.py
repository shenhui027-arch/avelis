"""B_raw / B0 指纹：manifest.yaml 里可由代码算出的部分（不调用模型、不连数据库、不读 .env 的值）。

  uv run --project backend python experiments/agent-v2/fingerprint.py [--truth <ec-truth.json 路径>]

输出 JSON：提示与工具 schema 的 SHA-256、锁文件与评分器哈希、预算常量、模型请求参数（按代码常量推出）、
.env 里的键名（只列键名，不读值）。manifest.yaml 冻结时把这里的输出抄进去，并注明来源提交。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend" / "src"), str(ROOT / "backend"), str(ROOT / "scripts")]


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def file_sha(path: Path) -> str | None:
    return sha(path.read_bytes()) if path.exists() else None


def tool_schemas(explore: bool) -> str:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from avelis.analysis.tools import AnalysisToolbox

    box = AnalysisToolbox.__new__(AnalysisToolbox)
    box.explore_available = lambda: explore  # 只取 schema：是否带探索工具两种情况分别算
    return json.dumps([convert_to_openai_tool(t) for t in box.tools()], ensure_ascii=False, sort_keys=True)


def env_keys(path: Path) -> list[str]:
    """.env 的键名（不读值）。"""
    if not path.exists():
        return []
    return sorted({line.split("=", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()
                   if "=" in line and not line.lstrip().startswith("#")})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", type=Path, default=ROOT / ".demo" / "ec-truth.json")
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args()

    from langchain_core.tools import StructuredTool
    from langchain_core.utils.function_calling import convert_to_openai_tool

    import ec_data
    from avelis.analysis import agent
    from avelis.analysis.tools import Budget, Scope
    from avelis.config import Settings

    scope_tool = StructuredTool.from_function(lambda **kw: "", name="submit_scope", args_schema=Scope,
                                              description="提交问题的范围（只调用一次）")
    budget = Budget(queries=Settings.analysis_query_limit)
    out = {
        "prompts": {"system_template_sha256": sha(agent.SYSTEM_PROMPT),
                    "understand_sha256": sha(agent.UNDERSTAND_PROMPT),
                    "system_placeholder_sha256": sha(agent.system_message().content)},
        "tool_schemas": {"reason_with_explore_sha256": sha(tool_schemas(True)),
                         "reason_without_explore_sha256": sha(tool_schemas(False)),
                         "submit_scope_sha256": sha(json.dumps(convert_to_openai_tool(scope_tool), ensure_ascii=False,
                                                               sort_keys=True))},
        "locks": {"backend/uv.lock": file_sha(ROOT / "backend" / "uv.lock"), "uv.lock": file_sha(ROOT / "uv.lock"),
                  "frontend/pnpm-lock.yaml": file_sha(ROOT / "frontend" / "pnpm-lock.yaml")},
        "scorers": {"tests/eval/scoring.py": file_sha(ROOT / "backend" / "tests" / "eval" / "scoring.py"),
                    "tests/eval/scoring_v2.py": file_sha(ROOT / "backend" / "tests" / "eval" / "scoring_v2.py"),
                    "tests/eval/analysis_questions.yaml": file_sha(ROOT / "backend" / "tests" / "eval" /
                                                                   "analysis_questions.yaml")},
        "data": {"generator_version": ec_data.VERSION, "ec_data.py": file_sha(ROOT / "scripts" / "ec_data.py"),
                 "ec_truth_sha256": file_sha(args.truth)},
        "budget": {**budget.view()["limits"], "report_reserve": budget.report_reserve,
                   "revision_tool_calls": agent.REVISION_TOOL_CALLS,
                   "model_requests": getattr(budget, "model_requests", None),
                   "input_tokens": getattr(budget, "input_tokens", None),
                   "output_tokens": getattr(budget, "output_tokens", None)},
        "model_request": {"understand": {"effort": "off", "max_tokens": agent.UNDERSTAND_MAX_TOKENS,
                                         "tool_choice": "submit_scope"},
                          "reason": {"effort": "按指派（默认 low）", "max_tokens": None, "tool_choice": None},
                          "timeout_seconds": f"min({agent.MODEL_TIMEOUT_SECONDS}, 剩余时间)",
                          "max_retries": f"剩余时间 > {2 * agent.MODEL_TIMEOUT_SECONDS} 秒时 1，否则 0"},
        "settings": {"material_change_ratio": Settings.material_change_ratio,
                     "analysis_query_limit": Settings.analysis_query_limit,
                     "business_timezone": Settings.business_timezone, "query_max_rows": Settings.query_max_rows},
        "env_keys": env_keys(args.env),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
