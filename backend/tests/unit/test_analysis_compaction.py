"""确定性上下文压缩：用真实工具箱和现有模型替身检查发给模型的消息。"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from avelis.analysis import agent
from avelis.analysis.tools import Budget
from avelis.models import AnalysisRun, AnalysisStep
from tests.unit.test_analysis_hitl import reply
from tests.unit.test_analysis_live import interject, live_box, model_steps
from tests.unit.test_analysis_toolbox import governed


SUMMARY_ID = "analysis:context-summary"
THINKING = "PRIVATE_MODEL_REASONING_MUST_NOT_BE_SUMMARIZED"


def context_steps(box):
    return [step for step in box.session.added
            if isinstance(step, AnalysisStep) and step.kind == "context" and step.tool == "compact"]


def long_history(*, invalid=False):
    messages = [SystemMessage("固定安全与核验规则"), HumanMessage("最初问题、范围和系统预检：查看 D1")]
    for index in range(8):
        messages.extend([
            AIMessage(content=f"old-response-{index}:" + "旧过程" * 450,
                      additional_kwargs={"reasoning_content": THINKING + "推理" * 450},
                      tool_calls=[{"name": "query_metric", "args": {}, "id": f"old-{index}"}]),
            ToolMessage(content="旧工具输出" * 450, tool_call_id=f"old-{index}"),
        ])
    calls = [{"name": "search_metrics", "args": {}, "id": "recent-a"},
             {"name": "search_metrics", "args": {}, "id": "recent-b"}]
    last_ai = (AIMessage(content="最近同时调用两个工具", invalid_tool_calls=[
        {"name": call["name"], "args": "{broken", "id": call["id"], "error": "invalid JSON"}
        for call in calls]) if invalid else AIMessage(content="最近同时调用两个工具", tool_calls=calls))
    # 最后六条消息从第一个 ToolMessage 开始，压缩边界必须退回发起调用的 AIMessage。
    messages.extend([last_ai, ToolMessage(content='{"metrics": []}', tool_call_id="recent-a"),
                     ToolMessage(content='{"metrics": []}', tool_call_id="recent-b"),
                     AIMessage(content="最近结果"), HumanMessage("继续检查"),
                     AIMessage(content="准备提交"), HumanMessage("保留最近这些消息")])
    return messages


def assert_tool_groups_complete(messages):
    calls = {call["id"] for message in messages if isinstance(message, AIMessage)
             for call in [*(message.tool_calls or []), *(message.invalid_tool_calls or [])]}
    results = [message.tool_call_id for message in messages if isinstance(message, ToolMessage)]
    assert set(results) == calls and len(results) == len(calls)


@pytest.mark.parametrize("invalid", [False, True], ids=["tool-calls", "invalid-tool-calls"])
def test_auto_compacts_request_and_preserves_structured_evidence_and_tool_groups(monkeypatch, invalid):
    box, model, _, _, _ = live_box(monkeypatch, [AIMessage(content="继续分析")])
    box.budget = Budget(input_tokens=100_000)
    dataset = governed(box, ["地区", "销售额"], [["华东", "7"], ["华南", "8"]])
    box.confirmed_items = [{"id": "P1", "kind": "hypothesis", "content": "已确认的分析依据", "refs": []}]
    box._step("tool", "query_metric", {"purpose": "核对地区分布"}, "ok",
              {"dataset": "D1", "summary": "已完成地区取数"})
    box._step("model", "model", {}, "ok", {"text": "旧模型正文", "thinking": THINKING})
    pending = interject(box, "只看相同渠道，维持原有核验规则")
    messages = long_history(invalid=invalid)
    prefix, original_count = messages[:2], len(messages)

    assert agent._invoke(model, messages, box, context_window=20_000) is not None

    requested = model.seen[0]
    summary = next(message for message in requested if message.id == SUMMARY_ID)
    step = context_steps(box)[0]
    assert requested[:2] == prefix and len(requested) < original_count
    assert "D1" in summary.content and dataset.title in summary.content
    assert "销售额" in summary.content and '"row_count": 2' in summary.content
    assert "query_metric" in summary.content and "已完成地区取数" in summary.content
    assert "已确认的分析依据" in summary.content and pending.text in summary.content
    assert "引用数字仍须引用 D 编号" in summary.content and "重新查看对应数据集" in summary.content
    assert THINKING not in summary.content and "旧模型正文" not in summary.content
    assert all(THINKING not in str(message) for message in requested)
    assert_tool_groups_complete(requested)
    assert {message.tool_call_id for message in requested if isinstance(message, ToolMessage)} == {
        "recent-a", "recent-b"}
    assert box.datasets() == {"D1": dataset} and dataset.rows == [["华东", "7"], ["华南", "8"]]
    assert pending.consumed_at is not None and step.status == "ok"
    assert step.output["reason"] == "auto"
    assert step.output["before_tokens"] > 20_000 * 0.75
    assert step.output["after_tokens"] == box.budget.estimate_input_tokens(requested, force=True)
    assert step.output["after_tokens"] < step.output["before_tokens"]
    assert step.output["kept_messages"] == len(requested) - 1
    assert step.output["summarized_messages"] > 0


@pytest.mark.parametrize(("context_window", "input_limit"), [
    (None, 20_000), (20_000, None), (1_000_000, 20_000), (20_000, 1_000_000),
], ids=["budget-only", "window-only", "budget-is-smaller", "window-is-smaller"])
def test_auto_uses_the_smallest_known_window(monkeypatch, context_window, input_limit):
    box, model, _, _, _ = live_box(monkeypatch, [AIMessage(content="继续")])
    box.budget = Budget(input_tokens=input_limit)
    messages = long_history()
    assert agent._invoke(model, messages, box, context_window=context_window) is not None
    assert len(model.seen) == 1 and len(context_steps(box)) == 1
    assert context_steps(box)[0].output["reason"] == "auto"
    assert context_steps(box)[0].output["after_tokens"] < 20_000


def test_unknown_window_does_not_estimate_or_auto_compact(monkeypatch):
    box, model, _, _, _ = live_box(monkeypatch, [AIMessage(content="继续")])
    box.budget = Budget(input_tokens=None)
    messages = long_history()
    original = list(messages)
    monkeypatch.setattr(box.budget, "estimate_input_tokens",
                        lambda *args, **kwargs: pytest.fail("窗口与预算都未知时不应估算输入"))
    assert agent._invoke(model, messages, box) is not None
    assert model.seen[0] == original and context_steps(box) == []


def test_exactly_seventy_five_percent_does_not_auto_compact(monkeypatch):
    box, model, _, _, _ = live_box(monkeypatch, [AIMessage(content="继续")])
    box.budget = Budget(input_tokens=None)
    messages = [SystemMessage("sys"), HumanMessage("q"), *[AIMessage(content="x" * 10) for _ in range(8)]]
    estimate = box.budget.estimate_input_tokens(messages, force=True)
    assert estimate % 3 == 0
    assert agent._invoke(model, messages, box, context_window=estimate * 4 // 3) is not None
    assert context_steps(box) == [] and len(model.seen[0]) == 10


@pytest.mark.parametrize("has_history", [False, True], ids=["initial-input", "long-history"])
def test_manual_request_is_consumed_once_even_without_old_history(monkeypatch, has_history):
    box, model, _, _, _ = live_box(monkeypatch, [AIMessage(content="继续"), AIMessage(content="再继续")])
    box.budget = Budget(input_tokens=None)
    box.run.compact_requested = True
    messages = long_history() if has_history else [SystemMessage("安全规则"), HumanMessage("最初问题")]
    assert agent._invoke(model, messages, box) is not None
    assert box.run.compact_requested is False and len(context_steps(box)) == 1
    assert context_steps(box)[0].output["reason"] == "manual"
    assert agent._invoke(model, messages, box) is not None
    assert len(context_steps(box)) == 1 and len(model.seen) == 2
    assert box.consume_compact_request() is False


@pytest.mark.parametrize("limited_by", ["budget", "window"])
def test_still_over_the_limit_after_compaction_uses_existing_model_skip(monkeypatch, limited_by):
    box, model, _, _, _ = live_box(monkeypatch, [])
    box.budget = Budget(input_tokens=20_000 if limited_by == "budget" else None)
    box.budget.used_input_tokens = 19_999 if limited_by == "budget" else 0
    messages = long_history()
    context_window = None if limited_by == "budget" else 50
    assert agent._invoke(model, messages, box, context_window=context_window) is None
    compact = context_steps(box)[0]
    assert compact.output["after_tokens"] < compact.output["before_tokens"]
    skipped = model_steps(box)[0]
    assert skipped.status == "skipped" and skipped.output["error_code"] == "budget_tokens"
    assert skipped.output["estimated_input_tokens"] == compact.output["after_tokens"]
    assert box.budget._blocked_input_estimate == compact.output["after_tokens"]
    assert box.budget.used_model_requests == 0 and model.seen == []


def test_act_keeps_compacted_history_across_rounds_without_reinjecting_old_interjection(monkeypatch):
    report = {"title": "已检查", "summary": "仍需补充依据", "claims": [],
              "conclusion": "insufficient_evidence"}
    box, model, config, _, _ = live_box(monkeypatch, [
        reply("search_metrics", {}, "lookup-a"), reply("search_metrics", {}, "lookup-b"),
        reply("submit_report", report, "report"),
    ])
    box.budget = Budget(input_tokens=None)
    model.context_window = 20_000
    available = [tool for tool in box.tools() if tool.name in {"search_metrics", "submit_report"}]
    monkeypatch.setattr(box, "tools", lambda: available)
    messages = long_history()
    old_interjection = {"id": "analysis:interject:old", "text": "已注入的旧插话原文"}
    box.interjections.append(old_interjection)
    messages.insert(4, HumanMessage(old_interjection["text"], id=old_interjection["id"]))
    snapshots = []
    invoke = model.invoke

    def capture(current):
        snapshots.append(list(current))
        return invoke(current)

    monkeypatch.setattr(model, "invoke", capture)
    result = agent.act({"messages": messages}, config)
    assert result["phase"] == "verify" and box.report is not None and len(snapshots) == 3
    assert len(context_steps(box)) == 1
    for requested in snapshots:
        assert sum(message.id == SUMMARY_ID for message in requested) == 1
        assert all(message.id != old_interjection["id"] for message in requested)
        assert sum(old_interjection["text"] in str(message.content) for message in requested) == 1
        assert all("old-response-" not in str(message.content) and THINKING not in str(message)
                   for message in requested)
        assert_tool_groups_complete(requested)
    assert sum(message.id == SUMMARY_ID for message in result["messages"]) == 1
    assert len(result["messages"]) < len(messages)


def test_compaction_compares_full_estimates_when_actual_prefix_usage_is_reusable(monkeypatch):
    box, model, _, _, _ = live_box(monkeypatch, [AIMessage(content="继续")])
    box.budget = Budget(input_tokens=100_000)
    messages = long_history()
    messages[1].content += "问题" * 10_500
    full_before = box.budget.estimate_input_tokens(messages, full=True)
    box.budget.used_input_tokens = 20_000
    box.budget.remember_model_input(messages, 20_000)
    assert box.budget.estimate_input_tokens(messages) == 20_000
    assert full_before > 25_000

    assert agent._invoke(model, messages, box, context_window=25_000) is not None

    compact = context_steps(box)[0]
    assert compact.output["reason"] == "auto" and compact.output["summarized_messages"] > 0
    assert compact.output["before_tokens"] == full_before
    assert compact.output["after_tokens"] == box.budget.estimate_input_tokens(messages, full=True)
    assert 20_000 < compact.output["after_tokens"] < 25_000 < compact.output["before_tokens"]
    assert sum(message.id == SUMMARY_ID for message in model.seen[0]) == 1
    assert box.budget.used_model_requests == 1


def test_executor_commits_consumed_manual_request_before_model_and_does_not_repeat_it(monkeypatch):
    box, model, _, _, _ = live_box(monkeypatch, [AIMessage(content="继续"), AIMessage(content="再继续")])
    box.budget = Budget(input_tokens=None)
    box.executor = "test-executor"
    run = AnalysisRun(id=box.run.id, user_id=box.user.id, question="最初问题", status="running",
                      stop_requested=False, compact_requested=True, budget={"executor": box.executor}, checkpoint={})
    box.run = run
    box.session.store[(AnalysisRun, run.id)] = run
    commits, locks = [], []
    execute = box.session.execute

    def capture_execute(statement):
        if getattr(statement, "_for_update_arg", None) is not None:
            locks.append(run.compact_requested)
        return execute(statement)

    def capture_commit():
        commits.append((run.compact_requested, len(context_steps(box))))

    invoke = model.invoke

    def inspect_request(messages):
        assert run.compact_requested is False
        assert (False, 1) in commits and True in locks
        return invoke(messages)

    monkeypatch.setattr(box.session, "execute", capture_execute)
    monkeypatch.setattr(box.session, "commit", capture_commit)
    monkeypatch.setattr(model, "invoke", inspect_request)
    messages = [SystemMessage("安全与核验规则"), HumanMessage("最初问题")]
    assert agent._invoke(model, messages, box) is not None
    assert agent._invoke(model, messages, box) is not None
    assert len(model.seen) == 2 and len(context_steps(box)) == 1
    assert context_steps(box)[0].output["reason"] == "manual" and run.compact_requested is False
