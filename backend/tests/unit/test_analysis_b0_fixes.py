"""共同基线 B0 的底座修复（docs/agent-v2/diagnosis.md 第 4 节）：每项断言实际的状态与数据，不以返回 200 或出现关键字为据。

A. 理解阶段写了追问（Scope.clarification）或选了候选以外的指标：运行停在待澄清，保存可恢复的上下文，不继续预检与推理；
   恢复时预算接着计数。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver

from avelis.analysis import agent, service
from avelis.analysis.tools import Budget
from tests.unit.test_analysis_pipeline import WINDOW, Phased, catalog
from tests.unit.test_analysis_toolbox import make_box

CONTEXT = {"today": "2026-10-30", "tz": "UTC", "window": WINDOW}


def scope_reply(**scope):
    return AIMessage(content="", tool_calls=[{"name": "submit_scope", "args": {"question_type": "change", **scope},
                                              "id": "s1"}])


def run_graph(box, model, question="地区甲的甲量为什么下降？"):
    graph = agent.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "t1", "toolbox": box, "model": model}}
    graph.invoke(agent.initial_state(question, "2026-10-30", "UTC", WINDOW), config)
    return graph.get_state(config).values


# --- A. 澄清 ---------------------------------------------------------------------------------------

def test_clarification_stops_the_run_before_precheck_and_reasoning(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    box.run.window, box.run.question = dict(WINDOW), "地区甲的甲量为什么下降？"
    monkeypatch.setattr(box, "precheck", lambda scope: pytest.fail("待澄清时不应执行预检"))
    model = Phased([scope_reply(metric_codes=["m_a"], filters=[{"dimension": "地区", "values": ["甲"]}],
                                clarification="“甲量”是指件数还是金额？")])
    state = run_graph(box, model)
    assert state["phase"] == "clarify" and len(model.seen) == 1  # 只有理解一次模型请求，没有进入推理
    assert box.report is None and box.verification is None
    c = box.clarification
    assert c["question"] == "“甲量”是指件数还是金额？"
    # 可恢复的上下文：模型给出的范围（含原始参数）、原问题与窗口、此刻已用的预算
    assert c["scope"]["metric_codes"] == ["m_a"] and c["raw_scope"]["clarification"] == c["question"]
    assert c["resume"] == {"run_question": "地区甲的甲量为什么下降？", "window": WINDOW}
    assert c["budget"]["used"]["queries"] == 0 and c["budget"]["used"]["attempts"] == 0
    waiting = [s for s in box.session.added if getattr(s, "kind", None) == "clarify"]
    assert len(waiting) == 1 and waiting[0].status == "waiting" and waiting[0].output["question"] == c["question"]


def test_metric_codes_outside_the_candidates_are_not_dropped_silently(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    monkeypatch.setattr(box, "precheck", lambda scope: pytest.fail("待澄清时不应执行预检"))
    state = run_graph(box, Phased([scope_reply(metric_codes=["m_a", "m_zz"])]))
    assert state["phase"] == "clarify"
    assert box.plan["scope"]["metric_codes"] == ["m_a", "m_zz"]  # 原样保存，不删
    assert "m_zz" in box.clarification["question"] and "不在候选口径中：m_zz" in box.plan["problem"]


def test_scope_without_clarification_still_runs_the_precheck(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    seen = []
    monkeypatch.setattr(box, "precheck", lambda scope: seen.append(scope.metric_codes) or [])
    model = Phased([scope_reply(metric_codes=["m_a"], clarification="  "),
                    AIMessage(content="", tool_calls=[])])
    monkeypatch.setattr(agent, "act", lambda state, config: {"messages": [], "phase": "verify", "rounds": 1, "notes": []})
    monkeypatch.setattr(agent, "verify", lambda state, config: {"phase": "done"})
    state = run_graph(box, model)
    assert seen == [["m_a"]] and box.clarification is None and state["phase"] == "done"


def test_budget_restore_keeps_counting_after_clarification():
    budget = Budget(tool_calls=30, queries=20, seconds=300, explore=8)
    budget.used_tools, budget.reports_total, budget.used_calls = 5, 1, 7
    budget.used_queries, budget.used_explore = 4, 2
    view = budget.view()
    view["used"]["seconds"] = 40.0
    restored = Budget.restore(view)
    assert restored.view()["used"] | {"seconds": 40.0} == view["used"]  # 已用次数原样恢复，不清零
    assert (restored.used_queries, restored.used_explore, restored.reports_total) == (4, 2, 1)
    assert 259 <= restored.remaining() <= 260  # 已用的 40 秒从截止时间里扣除，等待用户的时间不计入
    assert restored.view()["limits"] == view["limits"]


def test_clarify_runs_end_the_job_normally_and_pause_scheduled_tasks():
    run = SimpleNamespace(status="clarify", error=None, report={"summary": "请确认指标"})
    assert service._outcome(run) == {"status": "succeeded", "reason": None, "run_status": "clarify"}
    assert service._outcome(SimpleNamespace(status="failed", error="x"))["status"] == "failed"
