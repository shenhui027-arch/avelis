"""E0：真实模型的协议冒烟（任务书第 12 节，最多 12 次请求；本脚本最多 7 次）。不含业务数据，需要授权才运行。

  uv run --project backend python experiments/agent-v2/e0_smoke.py            打印计划，不请求
  uv run --project backend python experiments/agent-v2/e0_smoke.py --run      执行（读取 .env 的 AVELIS_MODEL_* ，不打印密钥）
  用 --only 指定逗号分隔的检查项；after_checkpoint 必须同时选择 round_trip。

检查项（每项写明请求次数）：
1. 思考 low + 工具的两轮往返，回传 reasoning_content（2 次）：第二轮不得 400；记录 finish_reason、usage、推理 token、
   响应里是否有 reasoning_content；
2. 同样两轮但用通用 ChatOpenAI（不回传，B_raw 的实现）（最多 2 次）：按官方文档应在第二轮返回 400——验证文档说法，
   结果如实记录（若没有 400，说明服务端当前宽松，不代表协议可以不遵守）；
3. 第 1 项的消息经 LangGraph 序列化、反序列化后再请求一轮（1 次）：不得 400；
4. 关闭思考、强制调用 submit_scope 式的工具（1 次）：确认 tool_choice 与 thinking.type=disabled 同时生效；
5. 关闭思考、max_tokens=8 的回答（1 次）：finish_reason 应为 length，llm_protocol.protocol_problem 识别为截断。
结果写到 .demo/experiments/e0/<时间>.json；只保存是否出现字段、字段长度与 usage，不保存思考内容。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "backend" / "src")]
MAX_REQUESTS = 7
CHECKS = ("round_trip", "plain_client", "after_checkpoint", "forced_tool_thinking_off", "truncation")


class Counter:
    def __init__(self):
        self.n = 0

    def take(self):
        if self.n >= MAX_REQUESTS:
            raise SystemExit(f"超过 E0 冻结的请求上限 {MAX_REQUESTS}")
        self.n += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--only", help=f"只运行指定检查项，逗号分隔，默认全部：{','.join(CHECKS)}")
    args = parser.parse_args()
    selected = set(CHECKS) if args.only is None else {name.strip() for name in args.only.split(",") if name.strip()}
    if not selected:
        parser.error("--only 至少需要一个检查项")
    unknown = selected - set(CHECKS)
    if unknown:
        parser.error(f"--only 包含未知检查项：{','.join(sorted(unknown))}")
    if "after_checkpoint" in selected and "round_trip" not in selected:
        parser.error("after_checkpoint 依赖 round_trip，请在 --only 中同时选择 round_trip")
    if not args.run:
        print(__doc__)
        return 0
    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.tools import StructuredTool
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from pydantic import BaseModel

    import demo
    from avelis.llm_protocol import REASONING_FIELD, ReasoningRoundTripChatOpenAI, protocol_problem

    env = demo.load_env()
    base = {"model": env["AVELIS_MODEL_NAME"], "api_key": env["AVELIS_MODEL_API_KEY"],
            "base_url": env.get("AVELIS_MODEL_BASE_URL") or "https://api.deepseek.com", "timeout": 60, "max_retries": 0}

    class Args(BaseModel):
        value: str
    tool = StructuredTool.from_function(lambda value: value, name="echo", args_schema=Args, description="原样返回 value")
    count, out = Counter(), {"started": datetime.now().isoformat(timespec="seconds"), "model": base["model"], "checks": {}}

    def summary(reply) -> dict:
        meta = reply.response_metadata or {}
        return {"finish_reason": meta.get("finish_reason"), "model_name": meta.get("model_name"),
                "usage": (meta.get("token_usage") or {}), "has_reasoning": REASONING_FIELD in reply.additional_kwargs,
                "reasoning_chars": len(reply.additional_kwargs.get(REASONING_FIELD) or ""),
                "tool_calls": [c["name"] for c in reply.tool_calls], "problem": str(protocol_problem(reply) or "")}

    def two_rounds(cls) -> tuple[list, dict]:
        model = cls(**base, reasoning_effort="low").bind_tools([tool])
        messages = [HumanMessage("这是协议检测。请调用 echo 工具，参数 value 为 ok；拿到结果后用一句话回答“完成”。")]
        count.take()
        first = model.invoke(messages)
        messages += [first, *(ToolMessage(content="ok", tool_call_id=c["id"]) for c in first.tool_calls)]
        result = {"first": summary(first)}
        try:
            count.take()
            second = model.invoke(messages)
            result["second"] = summary(second)
            messages.append(second)
        except Exception as exc:  # noqa: BLE001 - 如实记录
            result["second_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return messages, result

    if "round_trip" in selected:
        messages, out["checks"]["round_trip"] = two_rounds(ReasoningRoundTripChatOpenAI)
    if "plain_client" in selected:
        _, out["checks"]["plain_client"] = two_rounds(ChatOpenAI)
    if "after_checkpoint" in selected:
        serde = JsonPlusSerializer()
        restored = serde.loads_typed(serde.dumps_typed({"m": messages}))["m"]
        try:
            count.take()
            reply = ReasoningRoundTripChatOpenAI(**base, reasoning_effort="low").bind_tools([tool]).invoke(
                [*restored, HumanMessage("再调用一次 echo，value 为 again。")])
            out["checks"]["after_checkpoint"] = summary(reply)
        except Exception as exc:  # noqa: BLE001
            out["checks"]["after_checkpoint"] = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    if "forced_tool_thinking_off" in selected:
        count.take()
        forced = ReasoningRoundTripChatOpenAI(**base, extra_body={"thinking": {"type": "disabled"}}, max_tokens=200).bind_tools(
            [tool], tool_choice="echo").invoke([HumanMessage("调用 echo，value 为 scope。")])
        out["checks"]["forced_tool_thinking_off"] = summary(forced)
    if "truncation" in selected:
        count.take()
        cut = ReasoningRoundTripChatOpenAI(**base, extra_body={"thinking": {"type": "disabled"}}, max_tokens=8).invoke(
            [HumanMessage("请用两百字介绍数据血缘。")])
        out["checks"]["truncation"] = summary(cut)
    out["requests"] = count.n
    path = demo.STATE / "experiments" / "e0" / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
