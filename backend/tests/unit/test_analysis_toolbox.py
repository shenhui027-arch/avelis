"""分析工具箱与分析图的审计修复（ADR 0007；审计 #2、#8、#9、#10、#15、#16、#18、#19）。

不连接数据库：用内存会话替身与按脚本回复的模型替身驱动真实的 AnalysisToolbox 与 agent 节点，
断言实际的计数、拒绝、产出的数据集与返回给模型的内容。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from avelis.analysis import agent, export, tools
from avelis.analysis.tools import AnalysisToolbox, Budget
from avelis.ask import service as ask_service
from avelis.models import Asset, Metric, MetricVersion, User


class MemorySession:
    def __init__(self, objects=(), scalars=None):
        self.store = {(type(o), o.id): o for o in objects}
        self.added: list = []
        self.scalars_value = list(scalars or [])
        self.identity_map: dict = {}
        self.expired: list = []

    def expire(self, obj):
        self.expired.append(obj)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        pass

    def get(self, cls, id_, **kwargs):
        return self.store.get((cls, id_))

    def scalar(self, stmt):
        return None

    def scalars(self, stmt):
        return list(self.scalars_value)

    def execute(self, stmt):
        return []


def make_box(*, budget=None, catalog=None, objects=(), scalars=None, monkeypatch):
    user = SimpleNamespace(id="u1", is_active=True)
    session = MemorySession([user, *objects], scalars)
    session.store[(User, "u1")] = user
    catalog = catalog or ask_service.Catalog([], {})
    monkeypatch.setattr(ask_service, "visible_catalog", lambda s, u, with_values: catalog)
    monkeypatch.setattr(tools.access, "roles_of", lambda s, u: {"analyst"})
    monkeypatch.setattr(tools.access, "domain_ids", lambda s, u: {"dA"})

    def visible(s, code, allowed):  # 与 ask.service.visible_metric 同一规则：域不可见时只返回指标
        if code not in catalog.versions:
            return None, None
        m, v = catalog.versions[code]
        return (m, v) if m.domain_id in allowed else (m, None)

    monkeypatch.setattr(ask_service, "visible_metric", visible)
    run = SimpleNamespace(id="r1", model_adapter="test", verification={})
    return AnalysisToolbox(session, user, run, catalog, budget or Budget())


def governed_meta(box, name, *, intent=None, version=None):
    """补齐真实受治理查询会保存的意图与锁定版本；无元数据夹具仍可直接构造。"""
    catalog = ask_service.visible_catalog(box.session, box.user, with_values=False)
    entry = next(((code, pair) for code, pair in catalog.versions.items() if pair[0].name == name), None)
    if entry:
        code, (_, current) = entry
        version = version or current
    else:
        code = f"fixture_metric_{len(catalog.versions) + 1}"
        metric = SimpleNamespace(id=code, code=code, name=name, domain_id="dA")
        version = version or SimpleNamespace(kind="base", expression="SUM(amount)", unit=None, unit_scale=1)
        catalog.versions[code] = (metric, version)
    if not getattr(version, "id", None):
        version.id = f"fixture-{code}-v1"
    box.session.store[(MetricVersion, version.id)] = version
    return {
        "intent": {"metric_codes": [code], "compare": "custom", **(intent or {})},
        "versions": {code: version.id},
        "metric_codes": [code],
        "time_column": None,
    }


def governed(box, columns, rows, *, intent=None, truncated=False):
    meta = governed_meta(box, columns[0], intent=intent) if intent else {"intent": {}, "time_column": None}
    return box._dataset("governed", "测试", columns, rows, truncated=truncated, meta=meta)


# --- #10 统一分发计数与截止时间 ---------------------------------------------------------


def test_dispatch_counts_every_attempt_before_validation(monkeypatch):
    box = make_box(budget=Budget(tool_calls=5), monkeypatch=monkeypatch)  # 最后 2 次留给 submit_report：其余工具 3 次
    assert "未知工具" in box.dispatch("no_such_tool", {})
    assert "参数结构不对：metric_codes" in box.dispatch("query_metric", {"metric_codes": "x"})  # 写出出错的字段
    assert "metrics" in box.dispatch("search_metrics", {})
    assert box.budget.used_tools == 3
    over = json.loads(box.dispatch("search_metrics", {}))  # 重复调用也计数
    assert "上限" in over["error"] and "留给 submit_report" in over["error"] and box.budget.used_tools == 3
    report = json.loads(
        box.dispatch(
            "submit_report", {"title": "t", "summary": "s", "claims": [], "conclusion": "insufficient_evidence"}
        )
    )
    assert report == {"ok": True} and box.report is not None  # 预留的次数里仍可提交


def test_report_reserve_allows_one_resubmission_after_a_parse_error(monkeypatch):
    """第 7 点第 5、10 条：预算里保留最后 2 次给 submit_report；参数先用 json_repair 只修语法，修不了才算解析失败，
    回传出错的位置并允许重交一次；修好的参数照常严格校验，步骤记录标明 repaired 并保留原文摘要。"""
    box = make_box(budget=Budget(tool_calls=3), monkeypatch=monkeypatch)
    box.budget.used_tools = 1
    for _ in range(3):  # 其余工具已用完（3 − 2）；被拒绝的调用不占用 submit_report 的预留次数
        assert "上限" in json.loads(box.dispatch("search_metrics", {}))["error"]
    first = json.loads(box.dispatch("submit_report", "报告如下：标题 t"))  # 不是 JSON：修不了
    assert "参数无法解析" in first["error"] and "还可以重交一次" in first["error"]
    broken = (
        '{"title": 标题, "summary": "s", "conclusion": "insufficient_evidence", '
        '"claims": [{"type": "inference", "parts": ["x"] "premise": "p"}]}'
    )  # 字符串没加引号、少了逗号
    second = json.loads(box.dispatch("submit_report", broken))
    assert second == {"ok": True} and box.report["title"] == "标题"
    step = box.session.added[-1]
    assert step.tool == "submit_report" and step.output["repaired"] is True and step.output["raw_excerpt"] == broken
    third = json.loads(
        box.dispatch(
            "submit_report", {"title": "t", "summary": "s", "claims": [], "conclusion": "insufficient_evidence"}
        )
    )
    assert "不能再提交" in third["error"]
    # 修订是新的一轮：核验没过进入修订后，submit_report 的预留次数重新计算（最终评测里修订后的报告曾被拒）
    box.verification = {"claims": [], "fields": [], "causes": [], "conclusion": {"problems": ["x"]}}
    box.report = {"claims": []}
    agent.revise({}, {"configurable": {"toolbox": box, "model": None}})
    revised = json.loads(
        box.dispatch(
            "submit_report", {"title": "t", "summary": "s", "claims": [], "conclusion": "insufficient_evidence"}
        )
    )
    assert revised == {"ok": True} and box.budget.used_reports == 1


def test_invalid_tool_calls_are_counted_and_answered(monkeypatch):
    box = make_box(budget=Budget(tool_calls=5), monkeypatch=monkeypatch)
    reply = AIMessage(
        content="",
        tool_calls=[],
        invalid_tool_calls=[
            {
                "name": "query_metric",
                "args": "{bad json",
                "id": "call_1",
                "error": "Expecting value",
                "type": "invalid_tool_call",
            }
        ],
    )

    class Model:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return reply

    out = agent._round({"messages": []}, box, Model())
    assert out[1].tool_call_id == "call_1" and "无法解析" in out[1].content and len(out) == 2
    assert box.budget.used_tools == 1


class CountingModel:
    """每次都只回复同一个工具调用的模型替身，记录被请求的次数。"""

    def __init__(self, name="search_metrics", args=None):
        self.calls, self.name, self.args = 0, name, args or {}

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        self.calls += 1
        return AIMessage(content="", tool_calls=[{"name": self.name, "args": self.args, "id": f"c{self.calls}"}])


def test_no_model_call_after_shared_deadline(monkeypatch):
    budget = Budget(seconds=60)
    budget.started -= 61  # 截止时间已过
    box = make_box(budget=budget, monkeypatch=monkeypatch)
    model = CountingModel()
    config = {"configurable": {"toolbox": box, "model": model}}
    state = {"messages": [HumanMessage("问题")]}
    agent.understand({"question": "问题", "context": {}}, config)  # 截止时间已过：理解阶段也不请求模型
    out = agent.act(state, config)
    assert model.calls == 0 and box.report is None
    assert agent.verify({"messages": []}, config)["phase"] == "done" and box.report["incomplete"]
    assert any("没有提交报告" in n for n in out["notes"])


def test_loop_ends_when_total_calls_are_used_up(monkeypatch):
    """第 7 点第 7 条：执行循环只在总调用次数（或截止时间）用完时结束，没有固定的宽限轮数：其他工具用完后循环继续，
    直到 submit_report 的预留次数也用完。"""
    box = make_box(budget=Budget(tool_calls=6), monkeypatch=monkeypatch)  # 其余工具 4 次，最后 2 次只能是 submit_report
    model = ScriptedModel([("search_metrics", {})] * 7 + [("submit_report", "not json")] * 2 + [("search_metrics", {})])
    config = {"configurable": {"toolbox": box, "model": model}}
    out = agent.act({"messages": [HumanMessage("问题")]}, config)
    assert box.budget.used_tools == 4 and box.budget.used_reports == 2 and box.budget.used_calls == 9
    assert model.calls == 9 and box.report is None  # 被拒绝的三次之后两次提交仍可用；都失败后循环结束
    notices = [m.content for m in out["messages"] if isinstance(m, HumanMessage)]
    assert sum("submit_report 提交报告" in n for n in notices) == 1  # 只剩预留次数时提示一次


class ScriptedModel:
    """按顺序回复给定工具调用的模型替身（每轮一个调用）。"""

    def __init__(self, calls):
        self.calls, self.script = 0, list(calls)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages):
        self.calls += 1
        name, args = self.script.pop(0)
        if isinstance(args, str):  # 参数无法解析：LangChain 放在 invalid_tool_calls
            return AIMessage(
                content="",
                tool_calls=[],
                invalid_tool_calls=[
                    {
                        "name": name,
                        "args": args,
                        "id": f"c{self.calls}",
                        "error": "Expecting value",
                        "type": "invalid_tool_call",
                    }
                ],
            )
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"c{self.calls}"}])


def governed_catalog(monkeypatch):
    """一个已发布口径 m_a 与替身查询管线：记录真正执行的查询次数。"""
    metric = SimpleNamespace(id="m1", code="m_a", name="指标甲", domain_id="dA")
    version = SimpleNamespace(
        id="v1", kind="base", expression="SUM(amount)", unit="元", unit_scale=1, dimensions=[], components=None
    )
    executed = []

    def start_answer(session, user, question, adapter, cache):
        return SimpleNamespace(id=f"a{len(executed) + 1}", intent=None)

    def fulfil(session, user, intent, answer, scoped):
        executed.append(intent)
        answer.status, answer.error_code, answer.message = "answered", None, None
        answer.result = {"columns": ["地区", "value"], "rows": [["甲", 10], ["乙", 5]]}
        answer.sql, answer.metric_version_ids, answer.freshness = "SELECT 1", ["v1"], None

    monkeypatch.setattr(tools.service, "start_answer", start_answer)
    monkeypatch.setattr(tools.service, "fulfil", fulfil)
    monkeypatch.setattr(tools.service, "finish_answer", lambda *a: None)
    return ask_service.Catalog([], {"m_a": (metric, version)}), executed


QUERY = {
    "metric_codes": ["m_a"],
    "start_date": "2026-09-01",
    "end_date": "2026-09-30",
    "group_by": ["地区"],
    "purpose": "按地区",
}


def test_identical_governed_query_reuses_the_dataset_without_charging(monkeypatch):
    """第 7 点第 7 条：同一次运行中与已有数据集完全相同的受治理查询直接返回那个数据集，不计查询额度；
    参数不同（这里多了过滤条件）的照常执行、计数。"""
    catalog, executed = governed_catalog(monkeypatch)
    box = make_box(budget=Budget(queries=2), catalog=catalog, monkeypatch=monkeypatch)
    monkeypatch.setattr(box, "_metric_sources", lambda code: (None, None, [], {}))  # 来源闭包可读
    first = json.loads(box.dispatch("query_metric", QUERY))
    again = json.loads(box.dispatch("query_metric", {**QUERY, "purpose": "换个说法再查一次"}))
    assert first["dataset"] == again["dataset"] == "D1" and again["reused"] is True and "不计查询额度" in again["note"]
    assert len(executed) == 1 and box.budget.used_queries == 1 and len(box.datasets()) == 1
    other = json.loads(box.dispatch("query_metric", {**QUERY, "filters": [{"dimension": "地区", "values": ["甲"]}]}))
    assert other["dataset"] == "D2" and len(executed) == 2 and box.budget.used_queries == 2
    # 额度用完后，相同的查询仍可复用；新的查询被拒
    assert json.loads(box.dispatch("query_metric", QUERY))["dataset"] == "D1"
    blocked = json.loads(box.dispatch("query_metric", {**QUERY, "group_by": []}))
    assert "查询额度已用完" in blocked["error"] and len(executed) == 2

    def revoked(code):  # 复用前撤权：来源闭包核对失败，不再交付已有数据集
        box._deny("没有权限读取指标的来源", metric_code=code)

    monkeypatch.setattr(box, "_metric_sources", revoked)
    denied = json.loads(box.dispatch("query_metric", QUERY))
    assert denied["status"] == "denied" and "dataset" not in denied and len(executed) == 2


def test_view_dataset_pages_saved_rows_and_records_without_query_budget(monkeypatch):
    box = make_box(budget=Budget(queries=0), monkeypatch=monkeypatch)
    rows = [[f"成员{i}", i] for i in range(65)]
    summary = {"total": "2080"}
    box._dataset(
        "explore",
        "已有数据",
        ["member", "value"],
        rows,
        truncated=True,
        meta={
            "summary": summary,
            "units": {"value": {"kind": "count", "unit": "个"}},
            "summary_units": {"total": {"kind": "count", "unit": "个"}},
            "assumption": True,
            "partial": True,
        },
    )

    def no_query(*args, **kwargs):
        pytest.fail("回看数据集不得重新执行查询")

    monkeypatch.setattr(box.session, "execute", no_query)
    monkeypatch.setattr(tools.service, "fulfil", no_query)
    first = json.loads(box.dispatch("view_dataset", {"ref": "D1"}))
    assert first["dataset"] == "D1" and first["columns"] == ["member", "value"]
    assert first["rows"] == rows[:60] and first["rows_shown"] == 60
    assert first["offset"] == 0 and first["next_offset"] == 60 and first["row_count"] == 65
    assert first["summary"] == summary and first["units"]["value"] == {"kind": "count", "unit": "个"}
    assert first["summary_units"] == {"total": {"kind": "count", "unit": "个"}}
    assert first["root_kinds"] == ["explore"] and first["inputs"] == []
    assert first["truncated"] is True and first["context_truncated"] is True
    assert first["marks"] == ["来源含探索查询（未经口径审核）", "来源含假设计算", "来源含部分结果"]
    page = json.loads(box.dispatch("view_dataset", {"ref": "D1", "offset": 60, "limit": 5}))
    assert page["rows"] == rows[60:] and page["next_offset"] is None
    empty = json.loads(box.dispatch("view_dataset", {"ref": "D1", "offset": 65}))
    assert empty["rows"] == [] and empty["rows_shown"] == 0 and empty["next_offset"] is None
    assert box.budget.used_queries == box.budget.used_explore == 0
    assert box.budget.used_tools == box.budget.used_calls == 3 and len(box.datasets()) == 1
    steps = [step for step in box.session.added if getattr(step, "kind", None) == "tool"]
    assert len(steps) == 3 and all(step.tool == "view_dataset" and step.status == "ok" for step in steps)
    assert steps[1].args == {"ref": "D1", "offset": 60, "limit": 5} and steps[1].output["rows"] == rows[60:]
    compacted = box.context_summary()
    assert "需要细节用 view_dataset 重新查看对应数据集" in compacted
    context = json.loads(compacted.split("\n", 1)[1].split("\n", 1)[0])
    assert context["datasets"][0]["summary"] == summary and context["datasets"][0]["rows"] == rows[:5]


@pytest.mark.parametrize(
    "args",
    [
        {"ref": "D2"},
        {"ref": "other-run:D1"},
        {"ref": "D1", "offset": -1},
        {"ref": "D1", "limit": 0},
        {"ref": "D1", "limit": 61},
    ],
)
def test_view_dataset_rejects_unknown_refs_and_invalid_pages(monkeypatch, args):
    box = make_box(budget=Budget(queries=0), monkeypatch=monkeypatch)
    governed(box, ["value"], [[1]])
    out = json.loads(box.dispatch("view_dataset", args))
    expected = "不在本次运行中" if args["ref"] != "D1" else "参数结构不对"
    assert expected in out["error"]
    assert box.budget.used_tools == 1 and box.budget.used_queries == 0
    assert box.session.added[-1].tool == "view_dataset" and box.session.added[-1].status == "error"


def test_query_quota_only_stops_query_tools(monkeypatch):
    """查询额度用完只停用查询类工具：循环继续，计算工具与 submit_report 照常可用（语法有错的报告参数修复后提交）。"""
    catalog, executed = governed_catalog(monkeypatch)
    box = make_box(budget=Budget(queries=1), catalog=catalog, monkeypatch=monkeypatch)
    box.budget.used_queries = 1
    rows = [["甲", 10, 8], ["乙", 5, 6]]
    box._dataset(
        "governed",
        "测试",
        ["地区", "指标甲", "指标甲·上一期"],
        rows,
        meta=governed_meta(box, "指标甲", intent={"compare": "previous_period"}),
    )
    broken = '{"title": 标题, "summary": "s", "claims": [], "conclusion": "insufficient_evidence"}'  # 修复后可用
    model = ScriptedModel(
        [
            ("query_metric", QUERY),
            ("contribution", {"dataset": "D1", "keys": ["地区"], "current": "指标甲", "previous": "指标甲·上一期"}),
            ("submit_report", broken),
        ]
    )
    config = {"configurable": {"toolbox": box, "model": model}}
    out = agent.act({"messages": [HumanMessage("问题")]}, config)
    tool_replies = [json.loads(m.content) for m in out["messages"] if type(m).__name__ == "ToolMessage"]
    assert (
        "查询额度已用完" in tool_replies[0]["error"] and "计算工具与 submit_report 仍可用" in tool_replies[0]["error"]
    )
    assert tool_replies[1]["dataset"] == "D2"  # 计算工具照常执行
    assert tool_replies[2] == {"ok": True} and box.report is not None and model.calls == 3 and executed == []
    notices = [m.content for m in out["messages"] if isinstance(m, HumanMessage)]
    assert sum("查询类工具" in n and "已停用" in n for n in notices) == 1


def test_model_request_timeout_follows_remaining_time(monkeypatch):
    seen = []

    class Resolved:
        model = None

        def factory(self, timeout, max_retries, max_tokens=None):
            seen.append((timeout, max_retries))
            return CountingModel(
                "submit_report", {"title": "t", "summary": "s", "claims": [], "conclusion": "insufficient_evidence"}
            )

    budget = Budget(seconds=300)
    budget.started -= 280  # 只剩约 20 秒
    box = make_box(budget=budget, monkeypatch=monkeypatch)
    agent._round({"messages": []}, box, Resolved(), only_report=True)
    assert len(seen) == 1 and seen[0][0] <= 20 and seen[0][1] == 0


# --- #8 截断 ----------------------------------------------------------------------------


def test_truncated_input_is_reported_and_rejected_by_full_set_calcs(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    ds = governed(box, ["store", "门店成交额", "门店成交额·上一期"], [["A", 10, 8], ["B", 5, 6]], truncated=True)
    view = box._view("D1", ds)
    assert view["truncated"] and "不是全集" in view["note"]
    for name, args in (
        (
            "contribution",
            {"dataset": "D1", "keys": ["store"], "current": "门店成交额", "previous": "门店成交额·上一期"},
        ),
        ("coverage", {"dataset": "D1", "keys": ["store"]}),
        ("describe", {"dataset": "D1", "column": "门店成交额"}),
        ("correlate", {"dataset": "D1", "x": "门店成交额", "y": "门店成交额·上一期"}),
        ("segment_compare", {"dataset": "D1", "segment": "store", "measures": ["门店成交额"]}),
        ("outliers", {"dataset": "D1", "value": "门店成交额", "keys": ["store"]}),
    ):
        assert "截断" in json.loads(box.dispatch(name, args))["error"], name
    total = box.dispatch(
        "derive", {"label": "合计", "formula": "a", "values": [{"name": "a", "dataset": "D1", "column": "门店成交额"}]}
    )
    assert "截断" in json.loads(total)["error"]  # 截断的数据集不能求和
    one = json.loads(
        box.dispatch(
            "derive",
            {
                "label": "A 的变化",
                "formula": "a - b",
                "values": [
                    {"name": "a", "dataset": "D1", "column": "门店成交额", "match": {"store": "A"}},
                    {"name": "b", "dataset": "D1", "column": "门店成交额·上一期", "match": {"store": "A"}},
                ],
            },
        )
    )
    assert one["summary"]["value"] == "2.0000" and one["truncated"]  # 单行计算可以做，截断标记沿依赖传下去
    assert box.datasets()["D2"].truncated


def test_truncated_dataset_count_ref_is_rejected():
    """截断的数据集不是全集：不能用 CountRef 写“共几家门店”（ADR 0009）。"""
    from avelis.analysis.verify import ground_report

    rows = [{"store": s} for s in ("A", "B", "C")]
    claim = {"type": "fact", "parts": ["共 ", {"dataset": "D1", "distinct": "store"}, " 家门店"]}
    full, ok = ground_report(
        {"title": "t", "summary": "s", "conclusion": "insufficient_evidence", "claims": [claim]},
        {"D1": {"rows": rows}},
        None,
    )
    cut, bad = ground_report(
        {"title": "t", "summary": "s", "conclusion": "insufficient_evidence", "claims": [claim]},
        {"D1": {"rows": rows, "truncated": True}},
        None,
    )
    assert ok["ok"] and full["claims"][0]["text"] == "共 3 家门店"
    assert not bad["ok"] and "已截断" in bad["claims"][0]["problems"][0] and cut["claims"][0]["text"] == "共 [?] 家门店"


# --- #9 日历折算的期间来自查询 -------------------------------------------------------------


def test_calendar_adjust_uses_dataset_query_window(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    governed(
        box,
        ["成交额", "成交额·对比期"],
        [[3000, 3100]],
        intent={
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
            "compare": "custom",
            "compare_start": "2026-08-01",
            "compare_end": "2026-08-31",
        },
    )
    # 模型额外传入的日期被忽略：本期仍是 9 月 30 天，不会算出“本期 1 天、日均 3000”
    out = json.loads(
        box.dispatch(
            "calendar_adjust",
            {
                "dataset": "D1",
                "current": "成交额",
                "previous": "成交额·对比期",
                "current_period": ["2026-09-01", "2026-09-01"],
                "previous_period": ["2026-08-01", "2026-08-31"],
            },
        )
    )
    rows = {r[0]: dict(zip(out["columns"], r)) for r in out["rows"]}
    assert rows["本期"]["days"] == 30 and rows["本期"]["daily"] == "100.0000" and rows["上期"]["days"] == 31
    wrong = json.loads(
        box.dispatch("calendar_adjust", {"dataset": "D1", "current": "成交额·对比期", "previous": "成交额"})
    )
    assert "不是该数据集的本期值" in wrong["error"]
    calc_only = json.loads(box.dispatch("calendar_adjust", {"dataset": "D2", "current": "days", "previous": "daily"}))
    assert "只能用于" in calc_only["error"]  # 计算产出的数据集没有可追溯的期间
    governed(
        box,
        ["成交额", "成交额·上一期"],
        [[3000, 3000]],
        intent={"start_date": "2026-09-01", "end_date": "2026-09-30", "compare": "previous_period"},
    )
    prev = json.loads(
        box.dispatch("calendar_adjust", {"dataset": "D3", "current": "成交额", "previous": "成交额·上一期"})
    )
    assert {r[0]: r[1] for r in prev["rows"]} == {"本期": 30, "上期": 30}  # 上一期：8 月 2 日至 31 日


# --- #2 data_health 与 #19 changes 的授权与范围 ---------------------------------------------


def _derived_catalog():
    d_metric = SimpleNamespace(id="mD", code="ec_d", name="派生", domain_id="dA")
    d_version = SimpleNamespace(
        id="vD",
        version_no=1,
        kind="derived",
        status="published",
        base_asset_id=None,
        components=[{"metric_id": "mB", "version_id": "vB", "metric_name": "组成 B"}],
    )
    return ask_service.Catalog([], {"ec_d": (d_metric, d_version)})


def _health_objects():
    comp = Metric(id="mB", code="ec_b", name="组成 B", domain_id="dB", owner_id="o", created_by="o")
    comp_v = MetricVersion(id="vB", version_no=1, metric_id="mB", kind="base", status="published", base_asset_id="a1")
    asset = Asset(id="a1", datasource_id="ds1", catalog_name="c", db_name="d", name="t", domain_id="dB")
    return [comp, comp_v, asset]


def test_data_health_checks_component_domain_and_source_closure(monkeypatch):
    runs = [SimpleNamespace(status="failed", started_at=datetime(2026, 9, 1), error="password=secret host=10.0.0.1")]
    box = make_box(catalog=_derived_catalog(), objects=_health_objects(), scalars=runs, monkeypatch=monkeypatch)
    visible = {"a1": True, "a2": True}
    closure = {"a1": SimpleNamespace(id="a1", domain_id="dB"), "a2": SimpleNamespace(id="a2", domain_id="dX")}
    monkeypatch.setattr(tools.service, "version_sources", lambda s, v: closure)
    monkeypatch.setattr(tools.access, "can_read_asset", lambda s, u, a, allowed=None: visible[a.id])
    monkeypatch.setattr(tools, "freshness_boundary", lambda s, ds: {"last_collected_at": None})
    args = {"metric_code": "ec_d", "start_date": "2026-09-01", "end_date": "2026-09-30"}

    monkeypatch.setattr(tools.access, "domain_ids", lambda s, u: {"dA"})  # 组成指标 B 的域不可见
    out = json.loads(box.dispatch("data_health", args))
    assert out["status"] == "denied" and "组成指标 组成 B" in out["error"] and "c.d.t" not in json.dumps(out)
    assert box.denials and box.denials[0]["metric_code"] == "ec_d"

    monkeypatch.setattr(tools.access, "domain_ids", lambda s, u: {"dA", "dB"})
    visible["a2"] = False  # 来源闭包中有不可见的上游表
    assert json.loads(box.dispatch("data_health", args))["status"] == "denied"

    visible["a2"] = True
    out = json.loads(box.dispatch("data_health", args))
    assert out["sources"][0]["recent_collections"] == [{"status": "failed", "started_at": "2026-09-01 00:00:00"}]
    assert "secret" not in json.dumps(out, ensure_ascii=False)  # 不返回数据源的原始错误
    # 上下文证据数据集（ADR 0009 第 3 点）：有 D 编号、固定结构，注明截取范围，也不含原始错误
    ds = box.datasets()[out["dataset"]]
    assert ds.kind == "context" and ds.columns == [
        "base_asset",
        "metric_status",
        "drift",
        "last_collected_at",
        "collection_status",
        "collection_started_at",
    ]
    assert ds.rows[0][4:] == ["failed", "2026-09-01T00:00:00"] and "最近 5 次采集" in ds.meta["scope"]
    assert ds.meta["asset_ids"] == ["a1", "a2"] and ds.meta["metric_version_ids"] == ["vB", "vD"]
    assert "secret" not in json.dumps(ds.rows, ensure_ascii=False)


def test_changes_with_metric_only_returns_its_sources(monkeypatch):
    from avelis.api import governance

    metric = SimpleNamespace(id="mS", code="sales", name="销售", domain_id="dA")
    version = SimpleNamespace(
        id="vS", version_no=1, kind="base", status="published", base_asset_id="a1", components=None
    )
    catalog = ask_service.Catalog([], {"sales": (metric, version)})
    box = make_box(catalog=catalog, scalars=[SimpleNamespace(id="cs1")], monkeypatch=monkeypatch)
    monkeypatch.setattr(tools.access, "domain_ids", lambda s, u: {"dA"})
    monkeypatch.setattr(
        tools.service, "version_sources", lambda s, v: {"a1": SimpleNamespace(id="a1"), "a0": SimpleNamespace(id="a0")}
    )
    monkeypatch.setattr(tools.access, "can_read_asset", lambda s, u, a, allowed=None: True)
    monkeypatch.setattr(
        governance,
        "change_set_view",
        lambda s, cs, full, user: {
            "created_at": "2026-09-02T00:00:00",
            "events": [
                {"kind": "column_added", "asset_id": "a0", "asset": "c.d.sales_src", "column": "x"},
                {"kind": "column_added", "asset_id": "ad", "asset": "c.d.ad_spend", "column": "y"},
            ],
            "impacts": [{"object_id": "vS"}, {"object_id": "ad"}],
        },
    )
    args = {"start_date": "2026-09-01", "end_date": "2026-09-30"}
    scoped = json.loads(box.dispatch("changes", {**args, "metric_code": "sales"}))
    assert [e["asset"] for e in scoped["change_sets"][0]["events"]] == ["c.d.sales_src"]
    assert scoped["change_sets"][0]["impacts"] == 1
    everything = json.loads(box.dispatch("changes", args))
    assert len(everything["change_sets"][0]["events"]) == 2
    # 上下文证据数据集：固定结构的事件行、固定排序（时间、类型、对象、项目），注明截取范围；只含当前范围内的事件
    scoped_ds, all_ds = box.datasets()[scoped["dataset"]], box.datasets()[everything["dataset"]]
    assert scoped_ds.kind == "context" and scoped_ds.columns == [
        "type",
        "at",
        "object",
        "item",
        "change",
        "impacts",
        "note",
    ]
    assert scoped_ds.rows == [["元数据变更", "2026-09-02T00:00:00", "c.d.sales_src", "x", "column_added", 1, None]]
    assert [r[2] for r in all_ds.rows] == ["c.d.ad_spend", "c.d.sales_src"] and "最多 30 个事件" in all_ds.meta["scope"]
    assert scoped_ds.meta["units"] == {"impacts": {"kind": "count"}}
    assert scoped_ds.meta["asset_ids"] == ["a0"] and scoped_ds.meta["metric_version_ids"] == []
    assert all_ds.meta["asset_ids"] == ["a0", "ad"]


# --- #16 引用编号 ------------------------------------------------------------------------


def test_report_refs_must_be_dataset_numbers(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    out = box.dispatch(
        "submit_report",
        {
            "title": "t",
            "summary": "s",
            "conclusion": "insufficient_evidence",
            "claims": [{"type": "fact", "parts": ["x"], "refs": ["<b>"]}],
        },
    )
    assert "参数结构不对：claims.0.refs.0" in out and box.report is None
    ok = box.dispatch(
        "submit_report",
        {
            "title": "t",
            "summary": "s",
            "conclusion": "insufficient_evidence",
            "claims": [{"type": "fact", "parts": ["x"], "refs": ["D12"]}],
        },
    )
    assert json.loads(ok) == {"ok": True}


def test_exports_escape_refs_and_neutralise_formulas():
    view = {
        "id": "r",
        "question": "q",
        "status": "succeeded",
        "datasets": [],
        "steps": [],
        "report": {
            "title": "t",
            "summary": "s",
            "claims": [{"type": "fact", "text": "a", "refs": ["<link href='x'>", "D1"]}],
        },
        "verification": {
            "ok": False,
            "unverified": 1,
            "claims": [{"index": 0, "status": "unverified", "problems": ["文字片段里出现数字“7”"]}],
            "fields": [{"field": "summary", "status": "unverified", "problems": ["文字片段里出现数字“999”"]}],
            "unverified_fields": 1,
        },
    }
    md = export.to_markdown(view, audience="full")
    assert "<link" not in md and "D1（另有 1 个无效引用）" in md and "摘要：文字片段里出现数字“999”" in md
    assert "a" in md and "文字片段里出现数字“7”" in md and "⚠" in md
    assert export.to_pdf(view, audience="full").startswith(b"%PDF")  # 畸形标签不会让 PDF 解析失败
    csv = (
        export.to_csv(["=cmd", "值"], [['=HYPERLINK("http://x")', -5], ["@SUM(A1)", "-12.5"], ["\tx", None], ["+1", 3]])
        .decode("utf-8-sig")
        .splitlines()
    )
    assert csv == ["'=cmd,值", '"\'=HYPERLINK(""http://x"")",-5', "'@SUM(A1),-12.5", "'\tx,", "+1,3"]


# --- #18 cron ----------------------------------------------------------------------------


def test_cron_leap_day_is_valid_and_impossible_date_is_rejected():
    from avelis.analysis import service
    from avelis.governance.errors import Invalid

    after = datetime(2026, 10, 30)  # UTC
    leap = service.next_run("0 9 29 2 *", after)
    assert leap == datetime(2028, 2, 29, 1, 0)  # 业务时区 9:00 = UTC 1:00
    assert service.next_run("30 8 * * 1", after) == datetime(2026, 11, 2, 0, 30)
    with pytest.raises(Invalid, match="不会触发"):
        service.next_run("0 9 30 2 *", after)
    assert date(2028, 2, 29).weekday() == 1


# --- 复核补充 --------------------------------------------------------------------------


def test_authorization_expires_loaded_objects_and_skips_full_catalog(monkeypatch):
    """纯计算工具只核对账号与角色，不建全量口径目录；授权前让会话里的口径对象过期（复核：N+1 与旧状态）。"""
    box = make_box(monkeypatch=monkeypatch)
    monkeypatch.setattr(ask_service, "visible_catalog", lambda *a, **k: pytest.fail("不应重建全量口径目录"))
    stale = Metric(id="mX", code="x", name="x", domain_id="dA", owner_id="o", created_by="o")
    box.session.identity_map[("Metric", "mX")] = stale
    governed(box, ["v"], [[1], [2]])
    out = json.loads(box.dispatch("describe", {"dataset": "D1", "column": "v"}))
    assert out["summary"]["count"] == 2 and stale in box.session.expired


def test_invisible_asset_in_describe_asset_and_lineage_counts_as_denial(monkeypatch):
    hidden = SimpleNamespace(id="a9", catalog_name="c", db_name="d", name="secret", domain_id="dZ")
    box = make_box(scalars=[hidden], monkeypatch=monkeypatch)
    monkeypatch.setattr(tools.access, "can_read_asset", lambda s, u, a, allowed=None: False)
    for name in ("describe_asset", "lineage"):
        out = json.loads(box.dispatch(name, {"qualified": "c.d.secret"}))
        assert out["status"] == "denied", name
    assert [d["asset"] for d in box.denials] == ["c.d.secret", "c.d.secret"]
    box.session.scalars_value = []
    assert "不存在" in json.loads(box.dispatch("describe_asset", {"qualified": "c.d.none"}))["error"]
    assert len(box.denials) == 2  # 不存在只是错误，不是权限拒绝


def test_lmdi_treats_only_computation_precision_as_rounding():
    """因子都不变、总量从 1000 变成 1000.02：0.02 来自输入，不能分给最后一个因子（复核 P3）。"""
    from decimal import Decimal

    from avelis.analysis import calc

    out = calc.factor_decompose(("1000", "1000.02"), {"订单数": (1000, 1000), "客单价": (1, 1)})
    assert {f["factor"]: f["contribution"] for f in out["factors"]} == {
        "订单数": Decimal("0.00"),
        "客单价": Decimal("0.00"),
        "未解释差额": Decimal("0.02"),
    }
    # 大基数：两个因子都不变，总量多出 100（第三轮 P3：曾因相对 1e-9 的阈值约为 1000 而整体分给最后一个因子）
    big = calc.factor_decompose(("1000000000000", "1000000000100"), {"a": (1000000000000, 1000000000000), "b": (1, 1)})
    assert {f["factor"]: f["contribution"] for f in big["factors"]} == {
        "a": Decimal("0.00"),
        "b": Decimal("0.00"),
        "未解释差额": Decimal("100.00"),
    }
    exact = calc.factor_decompose(("5000", "4400"), {"订单数": (100, 80), "客单价": (50, 55)})
    assert exact["unexplained"] == 0 and abs(
        sum(f["contribution"] for f in exact["factors"]) - Decimal(-600)
    ) < Decimal("1e-10")


def test_pdf_chart_uses_saved_time_column():
    from avelis.analysis.figures import dataset_charts

    rows = [[f"2026-09-0{i}", "A", i * 10] for i in range(1, 6)]
    dataset = {
        "columns": ["时间段", "period", "成交额"],
        "rows": rows,
        "time_column": "时间段",
        "units": {"成交额": {"kind": "level"}},
    }
    assert dataset_charts(dataset)[0]["kind"] == "line"
    assert dataset_charts({**dataset, "time_column": None})[0]["kind"] != "line"


# --- 第三轮复核 -------------------------------------------------------------------------


class DocSession:
    """column_doc 用到的几类查询的内存替身：按查询的实体与条件返回。"""

    def __init__(self, assets, comments, edges=(), links=()):
        self.assets, self.comments, self.edges, self.links = {a.id: a for a in assets}, comments, edges, links

    def get(self, cls, id_, **kw):
        return self.assets.get(id_)

    def _params(self, stmt):
        return stmt.compile().params

    def scalar(self, stmt):
        entity, p = stmt.column_descriptions[0]["name"], self._params(stmt)
        if entity == "comment":
            return self.comments.get((p["asset_version_id_1"], p["name_1"]))
        if entity == "IdentityLink":
            return next((x for x in self.links if x.source_asset_id == p["source_asset_id_1"]), None)
        raise AssertionError(entity)

    def scalars(self, stmt):
        p = self._params(stmt)
        return [
            e
            for e in self.edges
            if e.target_asset_id == p["target_asset_id_1"] and e.target_column == p["target_column_1"]
        ]


def test_inherited_comment_only_from_visible_upstream():
    """A 域的视图继承 B 域表的注释：看不到 B 的人既看不到注释也看不到来源，状态为 missing（第三轮 #2）。"""
    from avelis.governance.column_docs import column_doc

    view = SimpleNamespace(
        id="v", kind="view", current_version_id="vv", domain_id="dA", catalog_name="c", db_name="dw", name="v_sales"
    )
    ext = SimpleNamespace(
        id="e",
        kind="external_table",
        current_version_id="ev",
        domain_id="dA",
        catalog_name="c",
        db_name="dw",
        name="e_sales",
    )
    secret = SimpleNamespace(
        id="s", kind="table", current_version_id="sv", domain_id="dB", catalog_name="c", db_name="hr", name="salary"
    )
    edge = SimpleNamespace(target_asset_id="v", target_column="amt", source_asset_id="s", source_column="amt")
    link = SimpleNamespace(source_asset_id="e", target_asset_id="s", status="confirmed")
    session = DocSession([view, ext, secret], {("sv", "amt"): "员工实发工资"}, edges=[edge], links=[link])
    only_a = lambda a: a.domain_id == "dA"  # noqa: E731
    for asset, visible_status in ((view, "inherited"), (ext, "linked")):
        hidden = column_doc(session, asset, "amt", can_see=only_a)
        assert hidden["status"] == "missing" and hidden["comment"] is None and hidden["source"] is None
        assert "salary" not in json.dumps(hidden)
        shown = column_doc(session, asset, "amt", can_see=lambda a: True)
        assert (
            shown["status"] == visible_status
            and shown["comment"] == "员工实发工资"
            and shown["source"] == "c.hr.salary.amt"
        )
    with pytest.raises(TypeError):  # 调用方必须给出可见性判断
        column_doc(session, view, "amt")


def test_worker_scan_continues_after_a_failing_step(monkeypatch, caplog):
    """一项扫描失败（如死锁被数据库中止）只记录日志，其余扫描照常进行，worker 主循环不退出（第三轮 #1）。"""
    from avelis import jobs
    from avelis.analysis import service as analysis_service

    calls = []

    def boom():
        raise RuntimeError("deadlock detected")

    monkeypatch.setattr(jobs, "abandoned_jobs", boom)
    monkeypatch.setattr(jobs, "schedule_due", lambda: calls.append("collect") or ["j1"])
    monkeypatch.setattr(analysis_service, "schedule_due", lambda: calls.append("analysis") or [])
    monkeypatch.setattr(jobs, "stale_jobs", lambda: calls.append("stale") or [])
    monkeypatch.setattr(jobs, "run_job", lambda job_id: calls.append(f"run {job_id}") or boom())
    monkeypatch.setattr(jobs, "purge_history_if_due", lambda: calls.append("purge"))
    jobs.scan_once()
    assert calls == ["collect", "analysis", "stale", "run j1", "purge"]
    assert "回收超时任务 失败" in caplog.text and "deadlock detected" in caplog.text


def units_dataset(box):
    """带量纲登记的受治理数据集（与 query_metric 登记的结构相同）。"""
    from avelis.analysis.measures import governed_units

    version = SimpleNamespace(kind="base", expression="SUM(amount)", unit="元", unit_scale=1)
    columns = ["区域", "成交额", "成交额·对比期", "成交额·变化", "成交额·变化率"]
    return box._dataset(
        "governed",
        "成交额 按区域 对比期",
        columns,
        [
            ["华东", "1969398.05", "2000000", "-30601.95", "-0.0153"],
            ["华南", "300071.87", "250000", "50071.87", "0.2003"],
        ],
        meta={
            **governed_meta(box, "成交额", version=version),
            "units": governed_units(columns, {"成交额": version}, "对比期"),
            "window": {"start": "2026-09-01", "end": "2026-09-30"},
        },
    )


def test_tools_register_units_and_summary_paths(monkeypatch):
    """ADR 0009：受治理查询、差额拆解、derive 登记量纲与单位；汇总路径登记后才能引用。"""
    box = make_box(monkeypatch=monkeypatch)
    units_dataset(box)
    assert box.datasets()["D1"].meta["units"]["成交额·变化"] == {"kind": "delta", "unit": "元", "additive": True}
    contribution = json.loads(
        box.dispatch(
            "contribution", {"dataset": "D1", "keys": ["区域"], "current": "成交额", "previous": "成交额·对比期"}
        )
    )
    meta = box.datasets()["D2"].meta
    assert contribution["dataset"] == "D2" and meta["units"]["change"] == {"kind": "delta", "unit": "元"}
    assert meta["summary_units"]["total.change_rate"] == {"kind": "rate_change"} and meta["explore"] is False
    ratio = json.loads(
        box.dispatch(
            "derive",
            {
                "label": "两期之比",
                "formula": "a / b",
                "values": [
                    {"name": "a", "dataset": "D1", "column": "成交额", "match": {"区域": "华南"}},
                    {"name": "b", "dataset": "D1", "column": "成交额·对比期", "match": {"区域": "华南"}},
                ],
            },
        )
    )
    assert ratio["summary"]["value"] == "1.20028748" and box.datasets()["D3"].meta["summary_units"]["value"] == {
        "kind": "ratio"
    }
    growth = json.loads(
        box.dispatch(
            "derive",
            {
                "label": "增长率",
                "formula": "a / b - 1",
                "values": [
                    {"name": "a", "dataset": "D1", "column": "成交额", "match": {"区域": "华南"}},
                    {"name": "b", "dataset": "D1", "column": "成交额·对比期", "match": {"区域": "华南"}},
                ],
            },
        )
    )
    assert growth["dataset"] == "D4" and box.datasets()["D4"].meta["summary_units"]["value"] == {"kind": "rate_change"}
    diff = json.loads(
        box.dispatch(
            "derive",
            {
                "label": "差额",
                "formula": "a - b",
                "values": [
                    {"name": "a", "dataset": "D1", "column": "成交额", "match": {"区域": "华南"}},
                    {"name": "b", "dataset": "D1", "column": "成交额", "match": {"区域": "华东"}},
                ],
            },
        )
    )
    assert (
        box.datasets()["D5"].meta["summary_units"]["value"] == {"kind": "delta", "unit": "元"}
        and diff["dataset"] == "D5"
    )


def test_derive_rejects_dead_inputs_and_marks_assumptions(monkeypatch):
    """derive：每个输入都必须影响结果；非白名单常量把结果标为假设计算（ADR 0009 第 3 点）。"""
    box = make_box(monkeypatch=monkeypatch)
    units_dataset(box)
    pick = {"name": "a", "dataset": "D1", "column": "成交额", "match": {"区域": "华南"}}
    dead = json.loads(box.dispatch("derive", {"label": "x", "formula": "a * 0 + 99999", "values": [pick]}))
    assert "不影响结果" in dead["error"] and len(box.datasets()) == 1
    guess = json.loads(box.dispatch("derive", {"label": "按转化率 0.3 估算", "formula": "a * 0.3", "values": [pick]}))
    assert box.datasets()["D2"].meta["assumption"] is True and "假设计算" in guess["summary"]["assumption"]
    scaled = json.loads(box.dispatch("derive", {"label": "万元", "formula": "a / 10000", "values": [pick]}))
    assert box.datasets()["D3"].meta["assumption"] is False and scaled["summary"]["value"] == "30.007187"
    zero = json.loads(box.dispatch("derive", {"label": "除零", "formula": "a / (a - a)", "values": [pick]}))
    assert "无法计算" in zero["error"]


@pytest.mark.parametrize(
    "parts, expected",
    [
        (
            ["华东线上订单数本期低于上期：抖音来源渠道骤降是线上降幅的主要来源"],
            "华东线上订单数本期低于上期：抖音来源渠道骤降是线上降幅的主要来源",
        ),
        (["华东成交额 ", {"dataset": "D1", "column": "成交额", "match": {"区域": "华东"}}], "华东成交额 196.94万元"),
    ],
)
def test_report_json_array_string_fields_use_parts_and_reference_verification(monkeypatch, parts, expected):
    box = make_box(monkeypatch=monkeypatch)
    units_dataset(box)
    fields = ("title", "summary", "unexplained", "method")
    encoded = " \n" + json.dumps(parts, ensure_ascii=False) + "\t "
    reply = box.dispatch(
        "submit_report", {**dict.fromkeys(fields, encoded), "conclusion": "insufficient_evidence", "claims": []}
    )
    assert json.loads(reply) == {"ok": True}
    saved_parts = {field: box.report[field] for field in fields}
    assert all(isinstance(value, list) and value[0] == parts[0] for value in saved_parts.values())
    agent.verify({}, {"configurable": {"toolbox": box, "model": None}})
    for field in fields:
        record = box.report["fields"][field]
        assert box.report[field] == record["text"] == expected
        assert record["parts"] == saved_parts[field]
        assert record["status"] == "verified" and record["errors"] == []
        assert [segment["kind"] for segment in record["segments"]] == (
            ["text"] if len(parts) == 1 else ["text", "value"]
        )
        if len(parts) > 1:
            assert record["segments"][1]["ref"] == "D1"
    assert box.verification["ok"] and box.verification["unverified_fields"] == 0
    assert box.verification["sources"]["direct"]["governed"] == (0 if len(parts) == 1 else len(fields))


@pytest.mark.parametrize(
    "column, expected, status",
    [
        ("成交额", "华东成交额 196.94万元", "verified"),
        ("不存在", "华东成交额 [?]", "unverified"),
    ],
)
def test_report_json_array_string_claim_parts_reach_reference_verification(monkeypatch, column, expected, status):
    box = make_box(monkeypatch=monkeypatch)
    units_dataset(box)
    parts = ["华东成交额 ", {"dataset": "D1", "column": column, "match": {"区域": "华东"}}]
    reply = box.dispatch(
        "submit_report",
        {
            "title": "分析结果",
            "summary": "已完成核对",
            "conclusion": "insufficient_evidence",
            "claims": [{"type": "fact", "parts": json.dumps(parts, ensure_ascii=False)}],
        },
    )
    assert json.loads(reply) == {"ok": True}
    saved_parts = box.report["claims"][0]["parts"]
    assert isinstance(saved_parts, list) and saved_parts[0] == parts[0]
    agent.verify({}, {"configurable": {"toolbox": box, "model": None}})
    claim = box.report["claims"][0]
    assert claim["text"] == expected and claim["parts"] == saved_parts
    assert claim["refs"] == ["D1"] and claim["explicit_refs"] == []
    assert claim["status"] == status
    assert box.verification["ok"] == (status == "verified")
    if status == "verified":
        assert claim["errors"] == [] and claim["segments"][1]["kind"] == "value"
        assert box.verification["status"] == "all_passed"
    else:
        assert "没有列 不存在" in claim["errors"][0]
        assert box.verification["status"] == "failed"


@pytest.mark.parametrize("field", ["title", "summary", "unexplained", "method", "claims.0.parts"])
@pytest.mark.parametrize("encoded", [False, True], ids=["list", "json-array-string"])
def test_report_decoded_parts_keep_pydantic_reference_validation(monkeypatch, field, encoded):
    box = make_box(monkeypatch=monkeypatch)
    parts = ["华东成交额 ", {"dataset": "D1", "column": "成交额", "precision": 5}]
    value = json.dumps(parts, ensure_ascii=False) if encoded else parts
    report = {"title": "分析结果", "summary": "已完成核对", "conclusion": "insufficient_evidence", "claims": []}
    if field == "claims.0.parts":
        report["claims"] = [{"type": "fact", "parts": value}]
    else:
        report[field] = value
    result = json.loads(box.dispatch("submit_report", report))
    assert "参数结构不对" in result["error"] and field in result["error"]
    assert "precision" in result["error"] and box.report is None


@pytest.mark.parametrize(
    "value, expected_parts, expected",
    [
        (" 华东[线上]订单下降 ", " 华东[线上]订单下降 ", " 华东[线上]订单下降 "),
        ("  [线上]订单下降  ", "  [线上]订单下降  ", "  [线上]订单下降  "),
        (' ["未闭合" ', ' ["未闭合" ', ' ["未闭合" '),
        ('["完整数组"] 后续说明', '["完整数组"] 后续说明', '["完整数组"] 后续说明'),
        ('{"title":"文字"}', '{"title":"文字"}', '{"title":"文字"}'),
        (json.dumps(['["内层文字"]'], ensure_ascii=False), ['["内层文字"]'], '["内层文字"]'),
    ],
)
def test_report_string_fields_preserve_plain_invalid_and_nested_json(monkeypatch, value, expected_parts, expected):
    box = make_box(monkeypatch=monkeypatch)
    fields = ("title", "summary", "unexplained", "method")
    reply = box.dispatch(
        "submit_report", {**dict.fromkeys(fields, value), "conclusion": "insufficient_evidence", "claims": []}
    )
    assert json.loads(reply) == {"ok": True}
    assert all(box.report[field] == expected_parts for field in fields)
    agent.verify({}, {"configurable": {"toolbox": box, "model": None}})
    for field in fields:
        record = box.report["fields"][field]
        assert box.report[field] == record["text"] == expected
        assert record["parts"] == expected_parts
        assert record["segments"] == [{"kind": "text", "part": 1, "text": expected}]
        assert record["status"] == "verified" and record["errors"] == []
    assert box.verification["ok"]


def test_report_parts_are_rendered_and_revision_lists_each_problem(monkeypatch):
    """ADR 0009：submit_report 保存片段列表；verify 渲染、做结构检查并保存每个字段的 parts / text / segments / status /
    errors；修订提示逐条写明第几条结论、第几个片段、什么原因。"""
    box = make_box(monkeypatch=monkeypatch)
    box.run.window = {
        "start": "2026-09-01",
        "end": "2026-09-30",
        "compare_start": "2026-08-01",
        "compare_end": "2026-08-31",
    }
    units_dataset(box)
    box.dispatch("contribution", {"dataset": "D1", "keys": ["区域"], "current": "成交额", "previous": "成交额·对比期"})
    huadong = {"dataset": "D1", "column": "成交额", "match": {"区域": "华东"}}
    ok = box.dispatch(
        "submit_report",
        {
            "title": [{"window": "run_current", "format": "month"}, "华东成交额下降"],
            "conclusion": "insufficient_evidence",
            "summary": ["华东成交额 ", huadong],
            "claims": [
                {
                    "type": "fact",
                    "parts": [
                        "华东 ",
                        huadong,
                        "，较上期下降 ",
                        {"dataset": "D1", "column": "成交额·变化", "match": {"区域": "华东"}, "direction": "down"},
                    ],
                },
                {
                    "type": "calc",
                    "parts": [
                        "合计增长 ",
                        {"dataset": "D2", "summary": "total.change", "direction": "down"},
                        " 元（D2）",
                    ],
                },
                {"type": "fact", "parts": ["西北 ", {"dataset": "D1", "column": "成交额", "match": {"区域": "西北"}}]},
                {"type": "inference", "parts": ["可能约 3 成来自促销"], "premise": "促销力度与上期相近"},
            ],
            "unexplained": "无",
        },
    )
    assert json.loads(ok) == {"ok": True}
    state = agent.verify({}, {"configurable": {"toolbox": box, "model": None}})
    report, verification = box.report, box.verification
    assert state == {"phase": "revise"} and verification["status"] == "failed"
    assert report["title"] == "2026 年 9 月华东成交额下降" and report["summary"] == "华东成交额 196.94万元"
    assert (
        report["fields"]["title"]["status"] == "verified" and report["fields"]["title"]["parts"][1] == "华东成交额下降"
    )
    first = report["claims"][0]
    assert (
        first["text"] == "华东 196.94万元，较上期下降 3.06万元"
        and first["refs"] == ["D1"]
        and first["status"] == "verified"
    )
    value = first["segments"][1]
    assert {
        k: value[k]
        for k in (
            "kind",
            "part",
            "ref",
            "path",
            "label",
            "raw",
            "text",
            "measure",
            "unit",
            "source",
            "explore",
            "precision",
            "direction",
            "role",
        )
    } == {
        "kind": "value",
        "part": 2,
        "ref": "D1",
        "path": "D1.成交额[区域=华东]",
        "label": "D1 · 成交额 · 区域=华东",
        "raw": "1969398.05",
        "text": "196.94万元",
        "measure": "level",
        "unit": "元",
        "source": "governed",
        "explore": False,
        "precision": None,
        "direction": "",
        "role": "fact",
    }
    assert [c["status"] for c in verification["claims"]] == ["verified", "unverified", "unverified", "unchecked"]
    calc_problems = verification["claims"][1]["problems"]
    assert calc_problems[0].startswith("第 2 个片段（D2.summary.total.change）：方向与正负不一致")
    assert len(calc_problems) == 1  # D2 已存在，文字里的数据集编号不再误报裸数字
    assert not any("紧跟单位" in p for p in calc_problems)  # 解析失败的引用不是数值段
    assert verification["claims"][2]["problems"] == [
        "第 2 个片段（D1.成交额[区域=西北]）：条件选中了 0 行，必须恰好一行（求和请先用 derive）"
    ]
    assert (
        report["claims"][2]["text"] == "西北 [?]" and verification["sources"]["direct"]["governed"] == 3
    )  # 摘要 1、第 1 条 2；日期另列
    assert verification["sources"]["windows"] == 1 and verification["sources"]["failed"] == 2
    message = agent.revise({}, {"configurable": {"toolbox": box, "model": None}})["messages"][0].content
    assert "- 第 2 条（calc）：合计增长 [?] 元（D2）" in message and "方向与正负不一致" in message
    assert "- 第 3 条（fact）：西北 [?]" in message and "第 4 条" not in message and "出现在" not in message
    assert box.report is None and box.budget.tool_calls == Budget().tool_calls + agent.REVISION_TOOL_CALLS


def test_sources_csv_keeps_reference_details_and_markdown_lists_used_datasets():
    """CSV 保留每个引用段及公式注入防护，Markdown 附录只列正文使用的数据与查询。"""
    value = {
        "kind": "value",
        "part": 2,
        "ref": "D1",
        "path": "D1.成交额[区域=华东]",
        "label": "D1 · 成交额 · 区域=华东",
        "raw": "1969398.05",
        "text": "196.94万元",
        "measure": "level",
        "unit": "元",
        "source": "governed",
        "explore": False,
        "precision": 2,
        "direction": "",
        "role": "fact",
    }
    window = {
        "kind": "window",
        "part": 1,
        "ref": "window",
        "path": "window.run_current",
        "label": "本期窗口",
        "raw": ["2026-09-01", "2026-09-30"],
        "text": "2026 年 9 月",
    }
    explore = {
        **value,
        "ref": "D6",
        "path": "D6.客流",
        "label": "D6 · 客流",
        "raw": "=cmd",
        "text": "=cmd",
        "measure": "unknown",
        "unit": "",
        "precision": None,
        "source": "explore",
        "explore": True,
        "role": "proposal",
    }
    report = {
        "title": "t",
        "summary": "s",
        "fields": {"title": {"segments": [window, {"kind": "text", "text": "下滑"}]}},
        "claims": [
            {
                "type": "fact",
                "text": "华东 196.94万元",
                "refs": ["D1"],
                "segments": [{"kind": "text", "text": "华东 "}, value],
            },
            {"type": "recommendation", "text": "=cmd", "refs": ["D6"], "segments": [explore]},
        ],
    }
    rows = export.sources_csv({"report": report}).decode("utf-8-sig").splitlines()
    assert rows == [
        "位置,类型,显示值,来源标签,数据集,取值路径,量纲,单位,小数位,方向,标记,原值",
        "标题,字段,2026 年 9 月,本期窗口,window,window.run_current,,,,,,2026-09-01 ~ 2026-09-30",
        "结论 1,事实,196.94万元,D1 · 成交额 · 区域=华东,D1,D1.成交额[区域=华东],水平值,元,2,,,1969398.05",
        "结论 2,建议,'=cmd,D6 · 客流,D6,D6.客流,未知,,,,探索查询（未经口径审核）；建议目标（非观测值）,'=cmd",
    ]
    datasets = [
        {
            "ref": "D1",
            "kind": "governed",
            "title": "查询：成交额",
            "columns": ["成交额"],
            "rows": [["1969398.05"]],
            "row_count": 1,
            "truncated": False,
            "units": {"成交额": {"kind": "level", "unit": "元"}},
            "sql": "SELECT 1969398.05 AS amount",
            "metric_version_ids": ["version-1"],
        },
        {
            "ref": "D2",
            "kind": "governed",
            "title": "未引用查询",
            "columns": ["成交额"],
            "rows": [["1"]],
            "units": {"成交额": {"kind": "level", "unit": "元"}},
            "sql": "SELECT 1 AS unused",
            "metric_version_ids": ["version-unused"],
        },
        {
            "ref": "D6",
            "kind": "explore",
            "title": "探索：客流",
            "columns": ["客流"],
            "rows": [["=cmd"]],
            "row_count": 1,
            "truncated": True,
            "units": {"客流": {"kind": "level"}},
            "sql": "SELECT '=cmd' AS flow",
        },
    ]
    view = {
        "id": "r",
        "question": "q",
        "status": "succeeded",
        "datasets": datasets,
        "steps": [],
        "report": report,
        "verification": {
            "ok": True,
            "status": "with_inference",
            "claims": [],
            "fields": [],
            "sources": {"direct": {"governed": 1, "explore": 1}, "with_explore": 1, "windows": 1},
        },
    }
    md = export.to_markdown(view, audience="full")
    assert "来源全部通过" in md and "未核实" in md
    assert "## 附录" in md and "### 用到的数据" in md and "数字来源" not in md
    assert "| D1 | 成交额 | 指标口径 | 1 | 否 |" in md
    assert "| D6 | 客流 | 临时查询，未经口径审核 | 1 | 是 |" in md
    assert "SELECT 1969398.05 AS amount" in md and "口径版本：version-1" in md
    assert "SELECT '=cmd' AS flow" in md and "口径版本：未登记" in md
    assert "SELECT 1 AS unused" not in md and "version-unused" not in md and "| D2 |" not in md
    assert "D6.客流" not in md
    assert "探索查询（未经口径审核）" not in md and "建议目标（非观测值）" not in md
    assert export.to_pdf(view, audience="full").startswith(b"%PDF")


def test_marks_follow_the_saved_dependency_closure(monkeypatch):
    """ADR 0009 第 6 点：来源、假设、截断沿服务端保存的依赖闭包传递；依赖缺失即拒绝；预览截断与结果截断分开标注。"""
    from avelis.analysis.tools import MODEL_ROWS

    box = make_box(monkeypatch=monkeypatch)
    explore = box._dataset(
        "explore",
        "探索：客流",
        ["门店", "客流", "客流·对比期"],
        [["A", "120", "100"], ["B", "80", "90"]],
        meta={"units": {"客流": {"kind": "level"}, "客流·对比期": {"kind": "level"}}},
    )
    units_dataset(box)  # D2 受治理
    box.dispatch("contribution", {"dataset": "D1", "keys": ["门店"], "current": "客流", "previous": "客流·对比期"})
    pick = {"name": "a", "dataset": "D3", "column": "change", "match": {"门店": "A"}}
    box.dispatch("derive", {"label": "估算", "formula": "a * 0.3", "values": [pick]})  # D4：假设计算
    box.dispatch(
        "derive",
        {
            "label": "再加工",
            "formula": "a + b",
            "values": [
                {"name": "a", "dataset": "D4", "column": "value", "match": {"name": "估算"}},
                {"name": "b", "dataset": "D2", "column": "成交额", "match": {"区域": "华东"}},
            ],
        },
    )  # D5：探索 + 口径混合
    ds = box.datasets()
    assert explore.meta["root_kinds"] == ["explore"] and explore.meta["explore"] is True
    assert ds["D3"].meta["closure"] == ["D1"] and ds["D3"].meta["explore"] is True
    assert ds["D4"].meta["assumption"] is True and ds["D4"].meta["closure"] == ["D1", "D3"]
    mixed = ds["D5"].meta
    assert mixed["closure"] == ["D1", "D2", "D3", "D4"] and mixed["root_kinds"] == ["explore", "governed"]
    assert mixed["explore"] is True and mixed["assumption"] is True  # 混合输入后标记不丢
    assert ds["D2"].meta["explore"] is False and ds["D2"].meta["closure"] == []
    with pytest.raises(tools.calc.CalcError, match="不在本次运行中"):
        box._dataset("calc", "x", ["v"], [[1]], inputs=["D9"])
    big = box._dataset("governed", "大表", ["k"], [[i] for i in range(MODEL_ROWS + 5)])
    view = box._view(f"D{big.seq}", big)
    assert view["context_truncated"] is True and "truncated" not in view and view["rows_shown"] == MODEL_ROWS
    marks = box._view("D5", ds["D5"])["marks"]
    assert marks == ["来源含探索查询（未经口径审核）", "来源含假设计算"]


def test_report_scope_copied_from_explain_change_matches_its_verdict(monkeypatch):
    """第 7 点第 9 条：报告照抄 explain_change 结果里的 summary.scope，登记键相同；没写的日期取本次分析的窗口。"""
    from avelis.analysis.grounding import scope_key

    box = make_box(monkeypatch=monkeypatch)
    box.run.window = {
        "start": "2026-09-01",
        "end": "2026-09-30",
        "compare_start": "2026-08-01",
        "compare_end": "2026-08-31",
    }
    copied = {
        "metric_code": "m_a",
        "filters": [{"dimension": "地区", "values": ["甲"]}],
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        "compare_start": "2026-08-01",
        "compare_end": "2026-08-31",
    }
    report = {"title": "t", "summary": "s", "claims": [], "conclusion": "insufficient_evidence"}
    assert json.loads(box.dispatch("submit_report", {**report, "scope": copied})) == {"ok": True}
    assert box.report["scope"] == copied and box.report["scope_key"] == scope_key(
        "m_a", copied["filters"], box.run.window
    )
    box.budget.used_reports = 0
    box.dispatch("submit_report", {**report, "scope": {"metric_code": "m_a", "filters": copied["filters"]}})
    assert box.report["scope"] == copied  # 窗口取本次分析的窗口


def test_each_model_call_records_latency_tokens_and_tool_calls(monkeypatch):
    """ADR 0012 度量：每次模型调用记一个 model 步骤：耗时、输入 / 输出 / 缓存命中的 token 数、本轮工具调用数。"""
    box = make_box(budget=Budget(tool_calls=5), monkeypatch=monkeypatch)
    reply = AIMessage(
        content="",
        tool_calls=[
            {"name": "search_metrics", "args": {}, "id": "c1"},
            {"name": "search_metrics", "args": {}, "id": "c2"},
        ],
        usage_metadata={"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280},
        response_metadata={
            "token_usage": {"prompt_cache_hit_tokens": 1024, "completion_tokens_details": {"reasoning_tokens": 30}}
        },
    )

    class Model:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return reply

    agent._round({"messages": []}, box, Model())
    steps = [s for s in box.session.added if getattr(s, "kind", None) == "model"]
    assert len(steps) == 1 and steps[0].seq == 1 and steps[0].duration_ms is not None  # 在这一轮的工具步骤之前
    assert steps[0].output == {
        "input_tokens": 1200,
        "output_tokens": 80,
        "cache_hit_tokens": 1024,
        "reasoning_tokens": 30,
        "tool_calls": 2,
        "tools": ["search_metrics", "search_metrics"],
        "text": "",
    }
    plain = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "input_token_details": {"cache_read": 8},
        },
    )
    assert agent.model_usage(plain) == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_hit_tokens": 8,
        "reasoning_tokens": None,
    }
    assert agent.model_usage(AIMessage(content="")) == {
        "input_tokens": None,
        "output_tokens": None,
        "cache_hit_tokens": None,
        "reasoning_tokens": None,
    }


def test_json_repair_never_invents_values():
    """修复只改语法：修复结果里的每个键与标量值都要在原文里原样出现，补出来的值（如空字符串）按解析失败处理。"""
    assert tools.repair_arguments('{"title": 标题, "summary": "s", "claims": []}') == {
        "title": "标题",
        "summary": "s",
        "claims": [],
    }
    assert tools.repair_arguments('{"region":}') is None  # 会被补成 ""：原文没有这个值
    assert tools.repair_arguments('{"a": 1, "b": [1, 2,], "c": true}') == {"a": 1, "b": [1, 2], "c": True}


def test_revision_resets_only_this_rounds_report_attempts(monkeypatch):
    """修订只重置本轮的 submit_report 尝试次数；累计的尝试仍占总预算，运行记录的调用次数不超过上限。"""
    box = make_box(budget=Budget(tool_calls=6), monkeypatch=monkeypatch)
    b = box.budget
    b.used_tools, b.used_reports, b.reports_total = 4, 2, 2  # 第一轮：其余工具用满，两次提交
    b.tool_calls += agent.REVISION_TOOL_CALLS
    b.used_reports = 0  # 修订：本轮重新计算
    assert b.exhausted() is None
    b.used_tools = 8  # 4 + 4：加上以前的两次提交，已到 12 − 2
    assert b.exhausted() and b.spent() is None  # 只能提交报告
    b.used_reports = b.reports_total = 4
    used, limits = b.view()["used"], b.view()["limits"]
    assert used["tool_calls"] == 12 <= limits["tool_calls"] and used["reports"] == 4


def test_query_already_scanned_by_explain_change_is_reused(monkeypatch):
    """模型按 explain_change 已扫描过的维度与两期分组取数：直接返回已有数据集，不计额度，权限照样复核。
    带排序或行数的请求不复用（扫描结果不满足“前 N 个”的语义，见 test_analysis_b0_fixes 的 F 组）。"""
    from avelis.analysis.tools import QueryArgs, _scan_key

    catalog, executed = governed_catalog(monkeypatch)
    box = make_box(budget=Budget(queries=5), catalog=catalog, monkeypatch=monkeypatch)
    monkeypatch.setattr(box, "_metric_sources", lambda code: (None, None, [], {}))
    scan = governed(box, ["地区", "指标甲", "指标甲·对比期"], [["甲", "10", "8"]])
    scan.meta["versions"] = {"m_a": "v1"}
    two = {**QUERY, "compare": "custom", "compare_start": "2026-08-01", "compare_end": "2026-08-31"}
    box._scanned[_scan_key(QueryArgs(**two))] = scan
    out = json.loads(box.dispatch("query_metric", two))
    assert out["dataset"] == f"D{scan.seq}" and out["reused"] and executed == [] and box.budget.used_queries == 0
    assert "explain_change 已按同样的指标、维度、窗口与筛选扫描过" in out["note"]
    top = json.loads(box.dispatch("query_metric", {**two, "order": "desc", "limit": 3}))
    assert top["dataset"] != f"D{scan.seq}" and len(executed) == 1 and executed[0].order == "desc"


def test_amount_per_order_is_a_level_not_a_ratio():
    """量纲推断：金额 / 订单数是每单均值（水平值，单位“元/单”），两期均值相减是差额，不能显示成百分比或“个百分点”
    （评测 e02 的 a/b - c/d 与 dc/oc）。同单位的两个水平值相除仍是比率。"""
    from avelis.analysis import measures

    units = {
        "a": {"kind": "level", "unit": "元"},
        "b": {"kind": "count", "unit": "单"},
        "c": {"kind": "level", "unit": "元"},
        "d": {"kind": "count", "unit": "单"},
    }
    assert measures.formula_unit("a/b - c/d", units) == {"kind": "delta", "unit": "元/单"}
    assert measures.formula_unit("a/b", units) == {"kind": "level", "unit": "元/单"}
    bare = {"a": {"kind": "level"}, "b": {"kind": "count"}}  # 探索查询的列常常没有单位
    assert measures.formula_unit("a/b", bare) == {"kind": "level"}
    assert measures.formula_unit("a/b - a/b", bare)["kind"] == "delta"
    assert measures.formula_unit("a/c", units) == {"kind": "ratio"}  # 同单位相除：比率


def test_zero_is_a_neutral_constant_but_insensitive_inputs_are_still_rejected():
    """常量白名单加入中性的 0；“每个输入都要影响结果”的检查保留：a*0+99999 仍被拒绝。"""
    from decimal import Decimal

    from avelis.analysis import measures
    from avelis.governance.derived import evaluate as evaluate_formula

    assert Decimal(0) in measures.CONSTANTS
    assert measures.insensitive("a*0+99999", {"a": Decimal(5)}, evaluate_formula) == ["a"]
