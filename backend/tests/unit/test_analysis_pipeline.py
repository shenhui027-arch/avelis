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
    # 候选外的指标 m_x 不再静默删除：停在待澄清（任务书 P0-A，见 test_analysis_b0_fixes）
    assert out["phase"] == "clarify" and out["scope"]["metric_codes"] == ["m_a", "m_x"]
    assert "不在候选口径中：m_x" in box.plan["problem"] and "m_x" in box.clarification["question"]
    assert box.plan["scope"]["filters"] == [{"dimension": "地区", "values": ["甲"]}]
    steps = [s for s in box.session.added if getattr(s, "tool", None) in ("model", "understand")]
    assert [(s.kind, s.tool) for s in steps] == [("model", "model"), ("plan", "understand"), ("clarify", "understand")]


def test_understand_failure_does_not_stop_the_analysis(monkeypatch):
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    model = Phased([AIMessage(content="随便说说")])
    out = agent.understand({"question": "问题", "context": {}}, {"configurable": {"toolbox": box, "model": model}})
    assert out["scope"] is None and out["notes"] == ["模型没有提交范围"] and box.plan == {"scope": None,
                                                                                    "problem": "模型没有提交范围"}


def test_precheck_runs_the_fixed_change_sequence_with_the_scope_filters(monkeypatch):
    """变化类预检：口径详情 → 变更 → 数据健康 → explain_change（带筛选）→ 日历折算 → 两期日趋势 → 覆盖度
    → 来源闭包的元数据与血缘；某一步失败只记下。"""
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
        {"dataset": f"D{box._dataset('calc', '日历折算', ['x'], [['1']]).seq}"}))

    def daily(**kw):
        calls.append(("query_metric", kw["time_grain"], kw["filters"], kw["start_date"]))
        ds = governed(box, ["period", "指标甲"], [["2026-08-01", "3"]])
        return json.dumps({"dataset": f"D{ds.seq}"})
    monkeypatch.setattr(box, "query_metric", daily)
    monkeypatch.setattr(box, "coverage", lambda **kw: calls.append(("coverage", kw["dataset"])) or json.dumps(
        {"dataset": "D5"}))
    monkeypatch.setattr(box, "_source_metadata", lambda code: calls.append(("metadata", code)) or [
        {"table": "c.d.t", "columns": [{"name": "x", "comment": "金额"}], "upstream": ["c.d.raw"]}])
    scope = Scope(question_type="change", metric_codes=["m_a"], filters=[{"dimension": "地区", "values": ["甲"]}])
    refs = box.precheck(scope)
    assert [c[0] for c in calls] == ["describe_metric", "changes", "data_health", "explain_change", "calendar_adjust",
                                     "query_metric", "coverage", "metadata"]
    assert calls[3][1] == [{"dimension": "地区", "values": ["甲"]}] and calls[3][2] == WINDOW["compare_start"]
    assert calls[4][1] == {"dataset": f"D{total.seq}", "current": "指标甲", "previous": "指标甲·对比期"}
    assert calls[5][1:] == ("day", [{"dimension": "地区", "values": ["甲"]}], WINDOW["compare_start"])  # 两期、带筛选
    assert refs == ["D1", "D2", "D3", "D4", "D5"] and box.precheck_notes == ["变更记录：变更记录不可用"]
    assert box.precheck_groups == {"attribution": ["D1", "D2", "D3"], "trend": ["D4", "D5"]}
    assert box.precheck_problem is None
    pack = box.context_pack()
    assert "口径：" in pack and "D2 自动归因" in pack and "summary：" in pack and "变更记录不可用" in pack
    assert pack.index("D2 自动归因") < pack.index("D4 测试") < pack.index("来源表 ")  # 判定与归因 → 日趋势 → 元数据


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
    box.precheck_notes = ["某一步：" + "失败原因" * 200]  # 很长的预检错误也受上限约束
    pack = box.context_pack()
    assert len(pack) <= box.PACK_CHARS and "项放不下" in pack and "只列前 8 行" in pack
    assert "预检中没有完成的步骤" in pack and pack.rstrip().endswith(("…", "D100"))


def test_system_prompt_is_stable_and_has_neutral_contrast_examples():
    """系统提示只放稳定内容（ADR 0012）：不含日期、窗口、预算；对照示例只用中性占位。"""
    text = agent.system_message().content
    assert "对照示例" in text and "<指标>" in text and "<成员甲>" in text
    assert "今天是" not in text and "分析窗口：本期" not in text and "预算：工具调用最多" not in text


def test_context_pack_truncates_metadata_before_trend_and_attribution(monkeypatch):
    """超出上限时按“判定与归因 → 日趋势与覆盖 → 元数据与血缘”的优先级截断；归因按维度各列前 3 个成员。"""
    box = make_box(budget=Budget(), monkeypatch=monkeypatch)
    rows = [[dim, f"{dim}{i}", "1", "2", "增加项"] for dim in ("地区", "渠道") for i in range(5)]
    attribution = box._dataset("calc", "自动归因", ["dimension", "member", "previous", "current", "role"], rows)
    trend = governed(box, ["period", "指标甲"], [[f"2026-09-{d:02d}", "1"] for d in range(1, 31)])
    box.precheck_groups = {"attribution": [f"D{attribution.seq}"], "trend": [f"D{trend.seq}"]}
    box.precheck_meta = [{"table": f"c.d.t{i}", "columns": [{"name": "x" * 50, "comment": "y" * 400}] * 8}
                         for i in range(10)]
    pack = box.context_pack()
    assert len(pack) <= box.PACK_CHARS and "D1 自动归因" in pack and "D2 测试" in pack
    assert "各维度只列前 3 个成员" in pack and "地区2" in pack and "地区3" not in pack
    assert "项放不下" in pack and "来源表 c.d.t9" in pack.split("项放不下")[1]  # 元数据最先被省略


def test_understanding_is_corrected_once_when_the_precheck_has_no_data(monkeypatch):
    """理解出的指标在预检里没有数据：把原因反馈给理解阶段重选一次（计入模型调用），第二次仍没有就照常进入推理。"""
    box = make_box(budget=Budget(), catalog=catalog(), monkeypatch=monkeypatch)
    config = {"configurable": {"toolbox": box, "model": None}}

    def precheck(scope):
        box.precheck_problem = "指标 m_a 在这个范围里没有可归因的数据"
        return []
    monkeypatch.setattr(box, "precheck", precheck)
    scope = {"question_type": "change", "metric_codes": ["m_a"]}
    first = agent.precheck({"scope": scope}, config)
    assert first["phase"] == "understand" and first["retried"] and "没有可归因的数据" in first["feedback"]
    assert agent.precheck({"scope": scope, "retried": True}, config)["phase"] == "reason"
    model = Phased([AIMessage(content="", tool_calls=[{"name": "submit_scope", "args": {"question_type": "change",
                                                                                         "metric_codes": ["m_b"]},
                                                       "id": "s2"}])])
    out = agent.understand({"question": "问题", "context": {}, "feedback": first["feedback"]},
                           {"configurable": {"toolbox": box, "model": model}})
    assert "上一次的范围在系统预检里没有数据：指标 m_a" in model.seen[0][-1].content and out["scope"]["metric_codes"] == ["m_b"]


def big_catalog(n_extra):
    """一个“类目销量”、一个“类目销售额”、两个同名“客单价”（不同业务域），再加 n_extra 个无关口径。"""
    def card(code, name, domain, dims=("地区",), description=""):
        return SimpleNamespace(code=code, name=name, aliases=[], status="published", description=description,
                               dimensions=[{"name": d, "label": d} for d in dims]), domain
    entries = [card("m_units", "类目销量", "dA", ("类目", "地区")), card("m_sales", "类目销售额", "dA", ("类目", "地区")),
               card("m_aov", "客单价", "dA"), card("x_aov", "客单价", "dB")]
    entries += [card(f"a{i:02d}", f"无关指标{i}", "dA") for i in range(n_extra)]
    version = SimpleNamespace(kind="base", expression="SUM(x)", unit=None, dimensions=[])
    return ask_service.Catalog([c for c, _ in entries], {
        c.code: (SimpleNamespace(id=c.code, name=c.name, domain_id=d), version) for c, d in entries})


def test_candidate_retrieval_scores_bigrams_and_carries_the_domain(monkeypatch):
    """候选检索：中文按字的二元组计分（问题里写“食品类目……销量”也能命中“类目销量”）；可见口径超过 30 个时取前 10 个，
    不超过时全部给出；每个候选带业务域，同名口径可以区分。不针对任何题目。"""
    from avelis.models import Domain

    cat = big_catalog(40)
    box = make_box(budget=Budget(), catalog=cat, objects=[SimpleNamespace(id="dA", name="域甲"),
                                                          SimpleNamespace(id="dB", name="域乙")], monkeypatch=monkeypatch)
    box.session.store[(Domain, "dA")] = SimpleNamespace(id="dA", name="域甲")
    box.session.store[(Domain, "dB")] = SimpleNamespace(id="dB", name="域乙")
    top = box.candidate_metrics("食品类目上月的销量为什么升高？")
    assert len(top) == 10 and top[0]["code"] == "m_units" and top[1]["code"] == "m_sales"
    aov = {c["code"]: c["domain"] for c in box.candidate_metrics("客单价为什么下降？")[:2]}
    assert aov == {"m_aov": "域甲", "x_aov": "域乙"}  # 同名口径带业务域
    small = make_box(budget=Budget(), catalog=big_catalog(10), monkeypatch=monkeypatch)
    assert len(small.candidate_metrics("完全无关的问题")) == 14  # 不超过 30 个：全部给出
