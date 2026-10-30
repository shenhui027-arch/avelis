"""回放用的假模型（ADR 0009 第 5 点）：按顺序返回录制的模型消息，取数类工具返回冻结的数据集。

轨迹文件（traces/*.json）按字段白名单保存：
- 顶层：version、name、question、window、messages，第 2 版另有 fixtures、expect；
- messages：每条只有 content 与 tool_calls（每个调用只有 name、args）。不保存工具结果原文、密钥、连接信息与模型的其他元数据；
- fixtures：取数类工具（FETCH_TOOLS）按调用顺序各一项 {tool, datasets}，数据集只有 DATASET_FIELDS，meta 只有 META_FIELDS
  （列、行、量纲、窗口、查询意图、来源标记），不保存 SQL、数据源与资产编号。计算类工具不录产出，回放时用冻结的数据重新计算；
- expect：真实运行的核验结论（status、ok、revised、conclusion）；
- report_schema：录制时 submit_report 参数的模式版本（tools.REPORT_SCHEMA），与当前版本不同的轨迹跳过并提示重新录制。
第 1 版是手工构造的轨迹（没有 fixtures，数据夹具在测试里冻结），按 ADR 0012 的流程：理解（submit_scope）→ 预检 →
推理 → 核验与修订；第 2 版由 scripts/record_replay_traces.py 从真实模型的运行录制（流程改版后不再录制，留作格式）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

TRACES = Path(__file__).with_name("traces")
TRACE_FIELDS = {"version", "report_schema", "name", "question", "window", "messages", "fixtures", "expect"}
MESSAGE_FIELDS = {"content", "tool_calls"}
CALL_FIELDS = {"name", "args"}
FIXTURE_FIELDS = {"tool", "datasets"}
DATASET_FIELDS = {"seq", "kind", "title", "inputs", "columns", "rows", "truncated", "meta"}
META_FIELDS = {"intent", "time_column", "units", "summary_units", "window", "summary", "column_evidence", "explore",
               "assumption", "partial", "preview", "purpose"}
EXPECT_FIELDS = {"status", "ok", "revised", "conclusion"}
# 取数类工具：结果来自数据库或元数据库，回放时返回冻结的数据集；其余工具（计算）用冻结的数据重新执行
FETCH_TOOLS = {"search_metrics", "describe_metric", "search_assets", "describe_asset", "lineage", "changes",
               "data_health", "query_metric", "query_sql", "sample_values", "explain_change"}


def dataset_fixture(ds) -> dict[str, Any]:
    """数据集的白名单视图（录制用）。"""
    return {"seq": ds.seq, "kind": ds.kind, "title": ds.title, "inputs": list(ds.inputs or []), "columns": list(ds.columns),
            "rows": ds.rows, "truncated": bool(ds.truncated),
            "meta": {k: v for k, v in (ds.meta or {}).items() if k in META_FIELDS}}


def load_trace(name: str) -> dict[str, Any]:
    trace = json.loads((TRACES / f"{name}.json").read_text(encoding="utf-8"))

    def only(obj: dict, allowed: set[str], where: str) -> None:
        extra = set(obj) - allowed
        assert not extra, f"轨迹 {name} {where}含白名单以外的字段：{extra}"
    only(trace, TRACE_FIELDS, "")
    for i, message in enumerate(trace["messages"]):
        only(message, MESSAGE_FIELDS, f"第 {i + 1} 条消息")
        for call in message.get("tool_calls") or []:
            only(call, CALL_FIELDS, f"第 {i + 1} 条消息的工具调用")
    for i, fixture in enumerate(trace.get("fixtures") or []):
        only(fixture, FIXTURE_FIELDS, f"第 {i + 1} 个取数夹具")
        assert fixture["tool"] in FETCH_TOOLS, f"轨迹 {name} 第 {i + 1} 个取数夹具的工具 {fixture['tool']} 不是取数类工具"
        for ds in fixture["datasets"]:
            only(ds, DATASET_FIELDS, f"第 {i + 1} 个取数夹具的数据集")
            only(ds["meta"], META_FIELDS, f"第 {i + 1} 个取数夹具的数据集 meta ")
    only(trace.get("expect") or {}, EXPECT_FIELDS, "expect ")
    return trace


class ScriptedModel:
    """按顺序返回轨迹里的消息；轨迹用完还被请求时直接失败（回放必须与图流程一一对应）。"""

    def __init__(self, messages: list[dict[str, Any]]):
        self.messages, self.calls, self.seen = list(messages), 0, []

    def bind_tools(self, tools, **kwargs):
        self.tools = [t.name for t in tools]
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        if self.calls >= len(self.messages):
            raise AssertionError(f"回放轨迹只有 {len(self.messages)} 条消息，图流程请求了第 {self.calls + 1} 条")
        message = self.messages[self.calls]
        self.calls += 1
        return AIMessage(content=message.get("content", ""), tool_calls=[
            {"name": c["name"], "args": c["args"], "id": f"replay_{self.calls}_{i}"}
            for i, c in enumerate(message.get("tool_calls") or [])])


def replay_fetches(box, fixtures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """让工具箱的取数类工具按顺序返回冻结的数据集（仍经统一分发计数）；返回剩余的夹具（回放结束时应为空）。

    取数类工具的每次调用（包括录制时参数无效、报错的调用）都录成一个夹具，回放时一律按顺序消耗，不按当前的工具模式
    重新校验参数：取数结果是冻结的，工具模式以后改了也能回放核验、修订这一段。"""
    pending = list(fixtures)
    original = box.dispatch

    def dispatch(name, args, *, only_report: bool = False):
        if name not in FETCH_TOOLS:
            return original(name, args, only_report=only_report)
        assert pending and pending[0]["tool"] == name, f"回放顺序不一致：模型调用 {name}，下一个夹具是 {pending[:1]}"
        fixture = pending.pop(0)
        box.budget.used_calls += 1
        box.budget.used_tools += 1
        box.budget.used_queries += name in ("query_metric", "query_sql", "sample_values")
        box.budget.used_explore += name in ("query_sql", "sample_values")
        last = None
        for ds in fixture["datasets"]:
            assert ds["seq"] == len(box.datasets()) + 1, f"数据集编号不一致：夹具 D{ds['seq']}，回放下一个 D{len(box.datasets()) + 1}"
            last = box._dataset(ds["kind"], ds["title"], ds["columns"], ds["rows"], inputs=ds["inputs"],
                                truncated=ds["truncated"], meta=ds["meta"])
        return json.dumps(box._view(f"D{last.seq}", last) if last else {"replayed": name}, ensure_ascii=False,
                          default=str)
    box.dispatch = dispatch
    return pending
