"""分析流程（ADR 0012）：理解阶段一次结构化输出、系统预检的固定排查、上下文包与稳定的系统提示。全部用替身，不连数据库与模型。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage

from avelis.analysis import agent
from avelis.analysis.tools import Budget, Scope
from avelis.ask import service as ask_service
from tests.unit.test_analysis_toolbox import governed, make_box

WINDOW = {"start": "2026-09-01", "end": "2026-09-30", "compare_start": "2026-08-01", "compare_end": "2026-08-31"}


def catalog():
    def card(code, name, aliases=()):
        return SimpleNamespace(code=code, name=name, aliases=list(aliases), status="published", description=None,
                               dimensions=[{"name": "地区", "label": "地区"}, {"name": "渠道", "label": "渠道"}])
    cards = [card("m_b", "指标乙"), card("m_a", "指标甲", ["甲量"]), card("m_c", "指标丙")]
    version = SimpleNamespace(kind="base", expression="SUM(x)", unit="件", dimensions=cards[0].dimensions)
    return ask_service.Catalog(cards, {c.code: (SimpleNamespace(id=c.code, name=c.name, domain_id="dA"), version)
                                       for c in cards})


class Phased:
    """记录 factory 收到的阶段参数，并按顺序返回给定回复的模型替身。"""

    def __init__(self, replies):
        self.replies, self.phases, self.tool_choice, self.seen = list(replies), [], [], []

    def factory(self, timeout, max_retries, **phase):
        self.phases.append(phase)
        return self

    def bind_tools(self, tools, **kwargs):
        self.tool_choice.append(kwargs.get("tool_choice"))
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0)


def test_understand_is_one_structured_call_without_thinking(monkeypatch):
    """理解：系统按问题文字给出候选口径（命中的排前面），模型用 submit_scope 一次输出范围；关闭思考、限制输出长度。"""
    cat = catalog()
    box = make_box(budget=Budget(), catalog=cat, monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    scope = {"question_type": "change", "metric_codes": ["m_a", "m_x"], "filters": [{"dimension": "地区", "values": ["甲"]}]}
    model = Phased([AIMessage(content="", tool_calls=[{"name": "submit_scope", "args": scope, "id": "s1"}])])
    out = agent.understand({"question": "地区甲的甲量为什么下降？", "context": {"today": "2026-10-30", "tz": "UTC",
                                                                         "window": WINDOW}},
                           {"configurable": {"toolbox": box, "model": model}})
    assert model.phases == [{"effort": "off", "max_tokens": agent.UNDERSTAND_MAX_TOKENS}]
    assert model.tool_choice == ["submit_scope"]
    candidates = json.loads(model.seen[0][-1].content.split("候选口径：", 1)[1].split("\n", 1)[0])
    assert [c["code"] for c in candidates] == ["m_a", "m_b", "m_c"]  # 别名“甲量”命中的排第一，其余按编码
    assert out["scope"]["metric_codes"] == ["m_a"] and "不在候选口径中：m_x" in out["notes"][0]
    assert box.plan["scope"]["filters"] == [{"dimension": "地区", "values": ["甲"]}]
    steps = [s for s in box.session.added if getattr(s, "tool", None) in ("model", "understand")]
    assert [s.tool for s in steps] == ["model", "understand"]


def test_understand_failure_does_not_stop_the_analysis(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    model = Phased([AIMessage(content="随便说说")])
    out = agent.understand({"question": "问题", "context": {}}, {"configurable": {"toolbox": box, "model": model}})
    assert out["scope"] is None and out["notes"] == ["模型没有提交范围"] and box.plan == {"scope": None,
                                                                                    "problem": "模型没有提交范围"}


def test_precheck_runs_the_fixed_change_sequence_with_the_scope_filters(monkeypatch):
    """变化类预检：口径详情 → 变更 → 数据健康 → explain_change（带筛选）→ 日历折算；某一步失败只记下。"""
    cat = catalog()
    box = make_box(budget=Budget(), catalog=cat, monkeypatch=monkeypatch)
    box.catalog = cat
    box.run.window = dict(WINDOW)
    calls = []
    total = governed(box, ["指标甲", "指标甲·对比期"], [["90", "100"]])

    def explain(**kw):
        calls.append(("explain_change", kw["filters"], kw["compare_start"]))
        ds = box._dataset("calc", "自动归因", ["member"], [["甲"]], inputs=["D1"],
                          meta={"summary": {"algorithm": "additive", "conclusion": "ranked_reasons"}})
        return json.dumps({"dataset": f"D{ds.seq}"})
    monkeypatch.setattr(box, "describe_metric", lambda code: calls.append(("describe_metric", code)) or json.dumps(
        {"code": code, "name": "指标甲", "kind": "base", "dimensions": []}))
    monkeypatch.setattr(box, "changes", lambda **kw: calls.append(("changes", kw["metric_code"])) or json.dumps(
        {"error": "变更记录不可用"}))
    monkeypatch.setattr(box, "data_health", lambda **kw: calls.append(("data_health", kw["metric_code"])) or json.dumps(
        {"dataset": "D1"}))
    monkeypatch.setattr(box, "explain_change", explain)
    monkeypatch.setattr(box, "calendar_adjust", lambda **kw: calls.append(("calendar_adjust", kw)) or json.dumps(
        {"dataset": "D3"}))
    scope = Scope(question_type="change", metric_codes=["m_a"], filters=[{"dimension": "地区", "values": ["甲"]}])
    refs = box.precheck(scope)
    assert [c[0] for c in calls] == ["describe_metric", "changes", "data_health", "explain_change", "calendar_adjust"]
    assert calls[3][1] == [{"dimension": "地区", "values": ["甲"]}] and calls[3][2] == WINDOW["compare_start"]
    assert calls[4][1] == {"dataset": f"D{total.seq}", "current": "指标甲", "previous": "指标甲·对比期"}
    assert refs == ["D1", "D2", "D3"] and box.precheck_notes == ["变更记录：变更记录不可用"]
    pack = box.context_pack()
    assert "口径：" in pack and "D2 自动归因" in pack and "summary：" in pack and "变更记录不可用" in pack


def test_precheck_relation_compares_segments_and_correlates(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    box.run.window = {"start": "2026-09-01", "end": "2026-09-30"}
    calls = []

    def query(**kw):
        calls.append(("query_metric", kw["group_by"], kw["metric_codes"]))
        ds = governed(box, [kw["group_by"][0], "指标甲", "指标乙"], [["x", "1", "2"]])
        return json.dumps({"dataset": f"D{ds.seq}"})
    monkeypatch.setattr(box, "query_metric", query)
    monkeypatch.setattr(box, "segment_compare", lambda **kw: calls.append(("segment_compare", kw["segment"],
                                                                           kw["measures"])) or "{}")
    monkeypatch.setattr(box, "correlate", lambda **kw: calls.append(("correlate", kw["x"], kw["y"])) or "{}")
    box.precheck(Scope(question_type="relation", metric_codes=["m_a", "m_b"], dimensions=["地区", "渠道", "其他"]))
    assert calls == [("query_metric", ["地区"], ["m_a", "m_b"]), ("segment_compare", "地区", ["指标甲", "指标乙"]),
                     ("correlate", "指标甲", "指标乙"), ("query_metric", ["渠道"], ["m_a", "m_b"]),
                     ("segment_compare", "渠道", ["指标甲", "指标乙"]), ("correlate", "指标甲", "指标乙")]


def test_context_pack_is_capped_and_lists_what_it_leaves_out(monkeypatch):
    box = make_box(budget=Budget(), monkeypatch=monkeypatch)
    for i in range(100):
        governed(box, ["成员", "值"], [[f"成员{j}", str(j)] for j in range(30)])
    pack = box.context_pack()
    assert len(pack) <= box.PACK_CHARS + 2000 and "其余数据集" in pack and "只列前 8 行" in pack


def test_system_prompt_is_stable_and_has_neutral_contrast_examples():
    """系统提示只放稳定内容（ADR 0012）：不含日期、窗口、预算；对照示例只用中性占位。"""
    text = agent.system_message().content
    assert "对照示例" in text and "<指标>" in text and "<成员甲>" in text
    assert "今天是" not in text and "分析窗口：本期" not in text and "预算：工具调用最多" not in text
