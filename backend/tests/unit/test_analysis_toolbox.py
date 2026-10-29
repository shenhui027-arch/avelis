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


def governed(box, columns, rows, *, intent=None, truncated=False):
    return box._dataset("governed", "测试", columns, rows, truncated=truncated,
                        meta={"intent": intent or {}, "time_column": None})


# --- #10 统一分发计数与截止时间 ---------------------------------------------------------

def test_dispatch_counts_every_attempt_before_validation(monkeypatch):
    box = make_box(budget=Budget(tool_calls=3), monkeypatch=monkeypatch)
    assert "未知工具" in box.dispatch("no_such_tool", {})
    assert "ValidationError" in box.dispatch("query_metric", {"metric_codes": "x"})  # 参数不符合 schema
    assert "ok" in box.dispatch("submit_plan", {"hypotheses": [], "steps": []})
    assert box.budget.used_tools == 3
    over = json.loads(box.dispatch("submit_plan", {"hypotheses": [], "steps": []}))  # 重复提交计划也计数
    assert "上限" in over["error"] and box.budget.used_tools == 3
    report = json.loads(box.dispatch("submit_report", {"title": "t", "summary": "s", "claims": []}))
    assert report == {"ok": True} and box.report is not None  # 只有最终报告不计数、预算用完后仍可提交


def test_invalid_tool_calls_are_counted_and_answered(monkeypatch):
    box = make_box(budget=Budget(tool_calls=5), monkeypatch=monkeypatch)
    reply = AIMessage(content="", tool_calls=[], invalid_tool_calls=[
        {"name": "query_metric", "args": "{bad json", "id": "call_1", "error": "Expecting value",
         "type": "invalid_tool_call"}])

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

    def __init__(self, name="submit_plan", args=None):
        self.calls, self.name, self.args = 0, name, args or {"hypotheses": [], "steps": []}

    def bind_tools(self, tools):
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
    agent.plan(state, config)
    out = agent.act(state, config)
    assert model.calls == 0 and box.report is None
    assert agent.verify({"messages": []}, config)["phase"] == "done" and box.report["incomplete"]
    assert any("没有提交报告" in n for n in out["notes"])


def test_repeated_plan_calls_exhaust_budget_then_grace_then_stop(monkeypatch):
    box = make_box(budget=Budget(tool_calls=4), monkeypatch=monkeypatch)
    model = CountingModel()
    config = {"configurable": {"toolbox": box, "model": model}}
    agent.act({"messages": [HumanMessage("问题")]}, config)
    assert box.budget.used_tools == 4  # 超过上限的调用一律被拒，不再计数
    assert model.calls == 4 + agent.GRACE_ROUNDS  # 用完后只给两轮宽限，之后不再请求模型
    assert box.report is None


def test_model_request_timeout_follows_remaining_time(monkeypatch):
    seen = []

    class Resolved:
        model = None

        def factory(self, timeout, max_retries):
            seen.append((timeout, max_retries))
            return CountingModel("submit_report", {"title": "t", "summary": "s", "claims": []})

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
    for name, args in (("contribution", {"dataset": "D1", "keys": ["store"], "current": "门店成交额",
                                         "previous": "门店成交额·上一期"}),
                       ("coverage", {"dataset": "D1", "keys": ["store"]}),
                       ("describe", {"dataset": "D1", "column": "门店成交额"}),
                       ("correlate", {"dataset": "D1", "x": "门店成交额", "y": "门店成交额·上一期"}),
                       ("segment_compare", {"dataset": "D1", "segment": "store", "measures": ["门店成交额"]}),
                       ("outliers", {"dataset": "D1", "value": "门店成交额", "keys": ["store"]})):
        assert "截断" in json.loads(box.dispatch(name, args))["error"], name
    total = box.dispatch("derive", {"label": "合计", "formula": "a", "values": [
        {"name": "a", "dataset": "D1", "column": "门店成交额"}]})
    assert "截断" in json.loads(total)["error"]  # 截断的数据集不能求和
    one = json.loads(box.dispatch("derive", {"label": "A 的变化", "formula": "a - b", "values": [
        {"name": "a", "dataset": "D1", "column": "门店成交额", "match": {"store": "A"}},
        {"name": "b", "dataset": "D1", "column": "门店成交额·上一期", "match": {"store": "A"}}]}))
    assert one["summary"]["value"] == "2.0000" and one["truncated"]  # 单行计算可以做，截断标记沿依赖传下去
    assert box.datasets()["D2"].truncated


def test_truncated_dataset_row_count_does_not_verify_totals():
    from avelis.analysis.verify import verify_report

    rows = [{"store": s} for s in ("A", "B", "C")]
    full = verify_report([{"type": "fact", "text": "共 3 家门店", "refs": ["D1"]}], {"D1": {"rows": rows}})
    cut = verify_report([{"type": "fact", "text": "共 3 家门店", "refs": ["D1"]}],
                        {"D1": {"rows": rows, "truncated": True}})
    assert full["ok"] and not cut["ok"]


# --- #9 日历折算的期间来自查询 -------------------------------------------------------------

def test_calendar_adjust_uses_dataset_query_window(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    governed(box, ["成交额", "成交额·对比期"], [[3000, 3100]],
             intent={"start_date": "2026-09-01", "end_date": "2026-09-30", "compare": "custom",
                     "compare_start": "2026-08-01", "compare_end": "2026-08-31"})
    # 模型额外传入的日期被忽略：本期仍是 9 月 30 天，不会算出“本期 1 天、日均 3000”
    out = json.loads(box.dispatch("calendar_adjust", {
        "dataset": "D1", "current": "成交额", "previous": "成交额·对比期",
        "current_period": ["2026-09-01", "2026-09-01"], "previous_period": ["2026-08-01", "2026-08-31"]}))
    rows = {r[0]: dict(zip(out["columns"], r)) for r in out["rows"]}
    assert rows["本期"]["days"] == 30 and rows["本期"]["daily"] == "100.00" and rows["上期"]["days"] == 31
    wrong = json.loads(box.dispatch("calendar_adjust", {"dataset": "D1", "current": "成交额·对比期",
                                                        "previous": "成交额"}))
    assert "不是该数据集的本期值" in wrong["error"]
    calc_only = json.loads(box.dispatch("calendar_adjust", {"dataset": "D2", "current": "days",
                                                            "previous": "daily"}))
    assert "只能用于" in calc_only["error"]  # 计算产出的数据集没有可追溯的期间
    governed(box, ["成交额", "成交额·上一期"], [[3000, 3000]],
             intent={"start_date": "2026-09-01", "end_date": "2026-09-30", "compare": "previous_period"})
    prev = json.loads(box.dispatch("calendar_adjust", {"dataset": "D3", "current": "成交额",
                                                       "previous": "成交额·上一期"}))
    assert {r[0]: r[1] for r in prev["rows"]} == {"本期": 30, "上期": 30}  # 上一期：8 月 2 日至 31 日


# --- #2 data_health 与 #19 changes 的授权与范围 ---------------------------------------------

def _derived_catalog():
    d_metric = SimpleNamespace(id="mD", code="ec_d", name="派生", domain_id="dA")
    d_version = SimpleNamespace(id="vD", kind="derived", status="published", base_asset_id=None, components=[
        {"metric_id": "mB", "version_id": "vB", "metric_name": "组成 B"}])
    return ask_service.Catalog([], {"ec_d": (d_metric, d_version)})


def _health_objects():
    comp = Metric(id="mB", code="ec_b", name="组成 B", domain_id="dB", owner_id="o", created_by="o")
    comp_v = MetricVersion(id="vB", metric_id="mB", kind="base", status="published", base_asset_id="a1")
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


def test_changes_with_metric_only_returns_its_sources(monkeypatch):
    from avelis.api import governance

    metric = SimpleNamespace(id="mS", code="sales", name="销售", domain_id="dA")
    version = SimpleNamespace(id="vS", kind="base", status="published", base_asset_id="a1", components=None)
    catalog = ask_service.Catalog([], {"sales": (metric, version)})
    box = make_box(catalog=catalog, scalars=[SimpleNamespace(id="cs1")], monkeypatch=monkeypatch)
    monkeypatch.setattr(tools.access, "domain_ids", lambda s, u: {"dA"})
    monkeypatch.setattr(tools.service, "version_sources",
                        lambda s, v: {"a1": SimpleNamespace(id="a1"), "a0": SimpleNamespace(id="a0")})
    monkeypatch.setattr(tools.access, "can_read_asset", lambda s, u, a, allowed=None: True)
    monkeypatch.setattr(governance, "change_set_view", lambda s, cs, full, user: {
        "created_at": "2026-09-02T00:00:00", "events": [
            {"kind": "column_added", "asset_id": "a0", "asset": "c.d.sales_src", "column": "x"},
            {"kind": "column_added", "asset_id": "ad", "asset": "c.d.ad_spend", "column": "y"}],
        "impacts": [{"object_id": "vS"}, {"object_id": "ad"}]})
    args = {"start_date": "2026-09-01", "end_date": "2026-09-30"}
    scoped = json.loads(box.dispatch("changes", {**args, "metric_code": "sales"}))
    assert [e["asset"] for e in scoped["change_sets"][0]["events"]] == ["c.d.sales_src"]
    assert scoped["change_sets"][0]["impacts"] == 1
    everything = json.loads(box.dispatch("changes", args))
    assert len(everything["change_sets"][0]["events"]) == 2


# --- #16 引用编号 ------------------------------------------------------------------------

def test_report_refs_must_be_dataset_numbers(monkeypatch):
    box = make_box(monkeypatch=monkeypatch)
    out = box.dispatch("submit_report", {"title": "t", "summary": "s",
                                         "claims": [{"type": "fact", "text": "x", "refs": ["<b>"]}]})
    assert "ValidationError" in out and box.report is None
    ok = box.dispatch("submit_report", {"title": "t", "summary": "s",
                                        "claims": [{"type": "fact", "text": "x", "refs": ["D12"]}]})
    assert json.loads(ok) == {"ok": True}


def test_exports_escape_refs_and_neutralise_formulas():
    view = {"id": "r", "question": "q", "status": "succeeded", "datasets": [], "steps": [],
            "report": {"title": "t", "summary": "s", "claims": [
                {"type": "fact", "text": "a", "refs": ["<link href='x'>", "D1"]}]},
            "verification": {"ok": False, "unverified": 1, "claims": [{"index": 0, "status": "unverified",
                                                                         "numbers": []}],
                             "fields": [{"field": "summary", "status": "unverified",
                                         "numbers": [{"text": "999", "verified": False}]}], "unverified_fields": 1}}
    md = export.to_markdown(view)
    assert "<link" not in md and "D1（另有 1 个无效引用）" in md and "摘要：999" in md
    assert export.to_pdf(view).startswith(b"%PDF")  # 畸形标签不会让 PDF 解析失败
    csv = export.to_csv(["=cmd", "值"], [["=HYPERLINK(\"http://x\")", -5], ["@SUM(A1)", "-12.5"],
                                        ["\tx", None], ["+1", 3]]).decode("utf-8-sig").splitlines()
    assert csv == ["'=cmd,值", "\"'=HYPERLINK(\"\"http://x\"\")\",-5", "'@SUM(A1),-12.5", "'\tx,", "+1,3"]


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
        "订单数": Decimal("0.00"), "客单价": Decimal("0.00"), "未解释差额": Decimal("0.02")}
    # 大基数：两个因子都不变，总量多出 100（第三轮 P3：曾因相对 1e-9 的阈值约为 1000 而整体分给最后一个因子）
    big = calc.factor_decompose(("1000000000000", "1000000000100"), {"a": (1000000000000, 1000000000000), "b": (1, 1)})
    assert {f["factor"]: f["contribution"] for f in big["factors"]} == {
        "a": Decimal("0.00"), "b": Decimal("0.00"), "未解释差额": Decimal("100.00")}
    exact = calc.factor_decompose(("5000", "4400"), {"订单数": (100, 80), "客单价": (50, 55)})
    assert exact["unexplained"] == 0 and sum(f["contribution"] for f in exact["factors"]) == Decimal(-600)


def test_pdf_chart_uses_saved_time_column():
    from reportlab.graphics.charts.lineplots import LinePlot

    rows = [[f"2026-09-0{i}", "A", i * 10] for i in range(1, 6)]
    d = {"columns": ["时间段", "period", "成交额"], "rows": rows, "time_column": "时间段"}
    font, _ = export.pdf_font()
    assert any(isinstance(c, LinePlot) for c in export._chart(d, font).contents)
    assert not any(isinstance(c, LinePlot) for c in export._chart({**d, "time_column": None}, font).contents)


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
        return [e for e in self.edges if e.target_asset_id == p["target_asset_id_1"]
                and e.target_column == p["target_column_1"]]


def test_inherited_comment_only_from_visible_upstream():
    """A 域的视图继承 B 域表的注释：看不到 B 的人既看不到注释也看不到来源，状态为 missing（第三轮 #2）。"""
    from avelis.governance.column_docs import column_doc

    view = SimpleNamespace(id="v", kind="view", current_version_id="vv", domain_id="dA", catalog_name="c",
                           db_name="dw", name="v_sales")
    ext = SimpleNamespace(id="e", kind="external_table", current_version_id="ev", domain_id="dA", catalog_name="c",
                          db_name="dw", name="e_sales")
    secret = SimpleNamespace(id="s", kind="table", current_version_id="sv", domain_id="dB", catalog_name="c",
                             db_name="hr", name="salary")
    edge = SimpleNamespace(target_asset_id="v", target_column="amt", source_asset_id="s", source_column="amt")
    link = SimpleNamespace(source_asset_id="e", target_asset_id="s", status="confirmed")
    session = DocSession([view, ext, secret], {("sv", "amt"): "员工实发工资"}, edges=[edge], links=[link])
    only_a = lambda a: a.domain_id == "dA"  # noqa: E731
    for asset, visible_status in ((view, "inherited"), (ext, "linked")):
        hidden = column_doc(session, asset, "amt", can_see=only_a)
        assert hidden["status"] == "missing" and hidden["comment"] is None and hidden["source"] is None
        assert "salary" not in json.dumps(hidden)
        shown = column_doc(session, asset, "amt", can_see=lambda a: True)
        assert shown["status"] == visible_status and shown["comment"] == "员工实发工资" and shown["source"] == "c.hr.salary.amt"
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
