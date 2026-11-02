(
    """确定性回放（ADR 0009 第 5 点）：假模型按轨迹返回消息，"""
    """驱动真实的分析图（理解 → 预检 → 推理 → 核验 → 修订，ADR 0012）。

轨迹文件按字段白名单保存（见 tests/replay/fake_model.py）。第 2 版从真实模型的运行录制：取数类工具返回冻结的数据集，
计算工具、核验与修订照常执行，必须得到与真实运行相同的核验结论；第 1 版是手工构造的轨迹，数据夹具在这里冻结。
固定三条轨迹：首轮通过、修订后通过、修订后仍失败。预算耗尽、中途撤权已有第 2 步的单元测试覆盖。
"""
)

from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from avelis.analysis import agent
from avelis.analysis.measures import governed_units
from avelis.analysis.tools import Budget
from tests.replay.fake_model import TRACES, ScriptedModel, load_trace, replay_fetches
from tests.unit.test_analysis_pipeline import catalog
from tests.unit.test_analysis_toolbox import governed_meta, make_box

FIXTURE_COLUMNS = ["地区", "指标甲", "指标甲·对比期", "指标甲·变化", "指标甲·变化率"]
FIXTURE_ROWS = [["甲", "120", "200", "-80", "-0.4000"], ["乙", "80", "100", "-20", "-0.2000"]]


def replay(name: str, monkeypatch):
    from types import SimpleNamespace

    from avelis.analysis.tools import REPORT_SCHEMA

    trace = load_trace(name)
    if trace["version"] >= 2 and trace.get("report_schema", 1) != REPORT_SCHEMA:
        pytest.skip(
            f"轨迹 {name} 录制于报告模式 v{trace.get('report_schema', 1)}，当前是 v{REPORT_SCHEMA}："
            "需要用 scripts/record_replay_traces.py 从新的评测运行重新录制"
        )
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    monkeypatch.setattr(agent.service, "version_sources", lambda session, version: {})  # 取数及来源在旧轨迹中已冻结。
    box.run.window = dict(trace["window"])
    pending: list = []
    if trace["version"] >= 2:  # 录制的轨迹：取数类工具按顺序返回冻结的数据集
        pending = replay_fetches(box, trace["fixtures"])
    else:
        version = SimpleNamespace(kind="base", expression="SUM(x)", unit="件", unit_scale=1)
        box._dataset(
            "governed",
            "指标甲 按地区 对比期",
            FIXTURE_COLUMNS,
            FIXTURE_ROWS,  # 冻结的数据夹具 D1
            meta={
                **governed_meta(box, "指标甲", version=version),
                "units": governed_units(FIXTURE_COLUMNS, {"指标甲": version}, "对比期"),
                "window": {"start": trace["window"]["start"], "end": trace["window"]["end"]},
            },
        )
    # 原轨迹保留；在理解之后补上 ADR 0013 的空提议，真实执行新增节点且不改变既有取数与核验消息。
    messages = [
        trace["messages"][0],
        {"tool_calls": [{"name": "submit_proposal", "args": {"items": []}}]},
        *trace["messages"][1:],
    ]
    model = ScriptedModel(messages)
    config = {"configurable": {"thread_id": f"replay:{name}", "toolbox": box, "model": model}, "recursion_limit": 50}
    graph = agent.build_graph(MemorySaver())
    graph.invoke(agent.initial_state(trace["question"], "2026-10-30", "Asia/Shanghai", trace["window"]), config)
    if trace["version"] >= 2:  # 与真实运行一致：模型回复与取数夹具全部用完，核验结论相同
        expect = trace["expect"]
        assert model.calls == len(messages) and not pending, (model.calls, pending[:1])
        v = box.verification
        assert (v["status"], v["ok"], v["revised"]) == (expect["status"], expect["ok"], expect["revised"]), v
        assert box.report["conclusion"] == expect["conclusion"]
    return box, model, trace


def feedback(model) -> list:
    return [m for m in model.seen[-1] if isinstance(m, HumanMessage) and m.content.startswith("核验发现以下问题")]


def test_first_pass(monkeypatch):
    box, model, trace = replay("first_pass", monkeypatch)
    v, report = box.verification, box.report
    assert v["ok"] and v["revised"] is False and not feedback(model)
    if trace["version"] >= 2:
        assert v["status"] in ("all_passed", "with_inference")
        return
    assert v["status"] == "all_passed" and model.calls == 4
    assert report["summary"] == "指标甲较上期下降 100件" and report["conclusion"] == "ranked_reasons"
    assert report["causes"][0]["status"] == "verified" and report["causes"][0]["contribution"]["text"] == "-80件"
    assert box.budget.used_tools == 1  # 只有拆解；理解阶段的 submit_scope 不经工具箱，submit_report 单独计数
    understand, reason = model.seen[0], model.seen[2]
    assert "候选口径" in understand[-1].content and "系统预检的上下文包" in reason[1].content
    assert "D1 指标甲 按地区 对比期" in reason[1].content  # 上下文包列出已有的数据集
    assert "2026-10-30" not in reason[0].content  # 系统提示只放稳定内容：日期在用户消息里


def test_revised_pass(monkeypatch):
    box, model, trace = replay("revised_pass", monkeypatch)
    v = box.verification
    assert v["ok"] and v["revised"] is True and len(feedback(model)) == 1  # 修订提示逐条写明问题
    if trace["version"] >= 2:
        return
    assert model.calls == 5 and "第 1 个片段（文字）出现数字“30”" in feedback(model)[0].content
    assert box.report["summary"] == "指标甲较上期下降 100件"
    first, last = model.seen[2], model.seen[-1]  # 修订提示追加在末尾：前缀不变，前缀缓存仍能命中
    assert last[: len(first)] == first and last[-1].content.startswith("核验发现以下问题")


def test_revised_fail(monkeypatch):
    box, model, trace = replay("revised_fail", monkeypatch)
    v = box.verification
    assert not v["ok"] and v["revised"] is True and v["status"] == "failed" and len(feedback(model)) == 1  # 只修订一轮
    if trace["version"] >= 2:
        return
    assert model.calls == 5
    assert v["unverified_fields"] == 1 and v["fields"][1]["field"] == "summary"
    assert box.report["summary"] == "指标甲较上期下降 30%，主要在地区甲"  # 如实保存，不静默改写


def test_trace_files_follow_the_field_whitelist(tmp_path, monkeypatch):
    from tests.replay import fake_model

    names = sorted(p.stem for p in TRACES.glob("*.json"))
    assert names == ["first_pass", "revised_fail", "revised_pass"]
    bad = json.loads((TRACES / "first_pass.json").read_text(encoding="utf-8"))
    bad["messages"][0]["response_metadata"] = {"token": "x"}
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setattr(fake_model, "TRACES", tmp_path)
    with pytest.raises(AssertionError, match="白名单以外的字段"):
        fake_model.load_trace("bad")
    leaky = json.loads((TRACES / "first_pass.json").read_text(encoding="utf-8"))
    leaky["fixtures"] = [
        {
            "tool": "query_metric",
            "datasets": [
                {
                    "seq": 1,
                    "kind": "governed",
                    "title": "t",
                    "inputs": [],
                    "columns": [],
                    "rows": [],
                    "truncated": False,
                    "meta": {"sql": "SELECT 1", "units": {}},
                }
            ],
        }
    ]
    (tmp_path / "leaky.json").write_text(json.dumps(leaky), encoding="utf-8")
    with pytest.raises(AssertionError, match="meta 含白名单以外的字段"):  # 数据集不保存 SQL
        fake_model.load_trace("leaky")
    model = ScriptedModel([])
    with pytest.raises(AssertionError, match="回放轨迹只有 0 条消息"):
        model.invoke([])
