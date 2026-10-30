"""自动归因 explain_change（ADR 0009 第 4 点）：计算逐项手算可核对；工具经统一分发调用，受治理查询用替身。

替身按真实查询管线的行为返回：带对比期的查询只含本期出现的成员（只在对比期出现的成员要靠补查对比期找到）。
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from avelis.analysis import attribution, tools
from avelis.analysis.grounding import scope_key
from avelis.analysis.tools import Budget
from avelis.models import Metric, MetricVersion
from tests.unit.test_analysis_toolbox import make_box

D = Decimal
WINDOW = {"start": "2026-09-01", "end": "2026-09-30", "compare_start": "2026-08-01", "compare_end": "2026-08-31"}


# --- 纯计算 ----------------------------------------------------------------------------

def test_applicability_from_expression():
    base = lambda expr: SimpleNamespace(kind="base", expression=expr)  # noqa: E731
    assert attribution.applicability(base("SUM(amount)"), {})["algorithm"] == "additive"
    assert attribution.applicability(base("count(*)"), {})["algorithm"] == "additive"
    for expr, word in (("COUNT(DISTINCT user_id)", "COUNT(DISTINCT"), ("AVG(price)", "AVG"), ("MAX(x)", "MAX"),
                       ("SUM(DISTINCT x)", "SUM(DISTINCT")):
        rule = attribution.applicability(base(expr), {})
        assert rule["algorithm"] is None and "不可加" in rule["reason"] or "无法确定" in rule["reason"], expr
    assert "无法确定可加性" in attribution.applicability(base("SUM(a) - SUM(b)"), {})["reason"]
    derived = SimpleNamespace(kind="derived", expression="num / den")
    rule = attribution.applicability(derived, {"num": base("SUM(a)"), "den": base("COUNT(*)")})
    assert rule == {"algorithm": "ratio", "reason": "", "numerator": "num", "denominator": "den"}
    assert "不可加" in attribution.applicability(derived, {"num": base("SUM(a)"), "den": base("AVG(b)")})["reason"]
    assert "不是 a / b" in attribution.applicability(SimpleNamespace(kind="derived", expression="num - den"), {})["reason"]


def test_additive_members_signed_ep_and_new_exit():
    rows = attribution.additive_members({("a",): (D(100), D(40)), ("b",): (None, D(30)), ("c",): (D(20), None)},
                                        D(-50))
    by = {r["member"]: r for r in rows}
    assert by["a"]["contribution"] == D(-60) and by["a"]["ep"] == D("1.2000")  # 可以超过 100%
    assert by["b"]["change_type"] == "新增" and by["b"]["previous"] == 0 and by["b"]["ep"] == D("-0.6000")
    assert by["c"]["change_type"] == "退出" and by["c"]["current"] == 0 and by["c"]["contribution"] == D(-20)


def test_ratio_members_structure_and_rate_effects_add_up():
    # 成员 a：b 份额 0.5 → 0.25；成员 b 只在本期出现（新增）；成员 c 只在对比期（退出）
    values = {("a",): (D(50), D(10), D(30), D(10)), ("b",): (None, None, D(60), D(20)), ("c",): (D(40), D(10), None, None)}
    totals = (D(90), D(20), D(90), D(30))  # R0 = 4.5，R1 = 3
    rows, covered = attribution.ratio_members(values, totals, D(-1.5))
    by = {r["member"]: r for r in rows}
    a = by["a"]  # w0 = .5, w1 = 1/3, r0 = 5, r1 = 3：结构 (1/3 − .5)·5，比率 1/3·(3 − 5)
    assert a["structure_effect"] == D("-0.8333") and a["rate_effect"] == D("-0.6667") and a["member_change"] == D("-2.0000")
    assert by["b"]["structure_effect"] == D("2.0000") and by["b"]["rate_effect"] is None  # w1·r1 = 2/3·3
    assert by["c"]["structure_effect"] == D("-2.0000") and by["c"]["change_type"] == "退出"  # −w0·r0 = −.5·4
    assert abs(covered - D(-1.5)) < D("1e-20")  # Σ贡献 = R1 − R0
    zero, _ = attribution.ratio_members({("z",): (D(5), D(0), D(5), D(5))}, (D(5), D(5), D(5), D(5)), D(1))
    assert zero[0]["contribution"] is None and "不可计算（分母为 0）" in zero[0]["change_type"]


def test_js_divergence_only_for_non_negative_distributions():
    assert attribution.js_divergence([D(1), D(1)], [D(1), D(1)]) == 0.0
    assert attribution.js_divergence([D(1), D(0)], [D(0), D(1)]) == 1.0
    assert attribution.js_divergence([D(1), D(-1)], [D(1), D(1)]) is None  # 不偷偷取绝对值
    assert attribution.js_divergence([D(0), D(0)], [D(1), D(1)]) is None


def test_judge_ranks_until_threshold_or_top_k_and_detects_offsetting():
    def dim(changes):
        total = sum(changes, D(0))
        rows = attribution.additive_members({(f"m{i}",): (D(100), D(100) + c) for i, c in enumerate(changes)}, total)
        return attribution.Dimension("维度", rows), total

    d, total = dim([D(-80), D(-15), D(-5)])
    out = attribution.judge(d, D(300), total, top_k=5)
    assert out.verdict == "ranked" and [r["member"] for r in out.selected] == ["m0"] and out.threshold_met is True
    d, total = dim([D(-30), D(-30), D(-20), D(-20)])
    out = attribution.judge(d, D(400), total, top_k=2)
    assert len(out.selected) == 2 and out.cumulative_ep == D("0.6000") and out.threshold_met is False
    d, total = dim([D(100), D(-95)])  # 相互抵消：|ΔT| / Σ|c| = 5 / 195 < 0.2
    out = attribution.judge(d, D(200), total, top_k=5)
    assert out.verdict == "offsetting" and [r["role"] for r in out.selected] == ["增加项", "减少项"]
    d, total = dim([D(1), D(0)])  # 成员变动 Σ|c| = 1 < 对比期 200 的 1%
    assert attribution.judge(d, D(200), total, top_k=5).verdict == "no_material_change"
    # 判定顺序（第 7 点第 8 条）：总变化不到阈值、成员变动达到阈值（一个成员大减、其他成员增长对冲）是相互抵消，
    # 不是没有实质变化；最大的减少项与增加项都列出
    d, total = dim([D(-120), D(60), D(59)])  # |ΔT| = 1 < 100（对比期 10000 的 1%），Σ|c| = 239 ≥ 100
    out = attribution.judge(d, D(10000), total, top_k=5)
    assert out.verdict == "offsetting"
    assert [(r["member"], r["role"]) for r in out.selected] == [("m1", "增加项"), ("m2", "增加项"), ("m0", "减少项")]
    d, total = dim([D(-60), D(30), D(29)])  # Σ|c| = 119 < 对比期 20000 的 1%：没有实质变化
    assert attribution.judge(d, D(20000), total, top_k=5).verdict == "no_material_change"
    # 没有候选维度（指标没有维度，或维度都被筛选掉了）：成员只有范围总量自己，按总变化判定
    assert attribution.verdict([], False, total_immaterial=True, candidates=False) == "no_material_change"
    assert attribution.verdict([], False, total_immaterial=False, candidates=False) == "ranked_reasons"
    assert attribution.verdict([], False, total_immaterial=False) == "insufficient_evidence"  # 有候选维度却没扫成
    ties = attribution.rank([attribution.Dimension("B", js=0.2), attribution.Dimension("A", js=0.2),
                             attribution.Dimension("C", js=None)])
    assert [x.name for x in ties] == ["A", "B", "C"]  # 并列按维度名，不可计算的排最后


# --- 工具 ------------------------------------------------------------------------------

class FakeQueries:
    """受治理查询替身：scenario[维度] = [(成员, 对比期值, 本期值), …]；比率指标为 (成员, a0, b0, a1, b1)。"""

    def __init__(self, box, names, scenario, *, totals=None, truncated=(), on_query=None):
        self.box, self.names, self.scenario, self.totals = box, names, scenario, totals
        self.truncated, self.on_query, self.calls = set(truncated), on_query, []

    def members(self, dim):
        return self.scenario[dim]

    def __call__(self, qa):
        box = self.box
        box.budget.used_queries += 1
        self.calls.append((tuple(qa.group_by), qa.compare))
        if self.on_query:
            self.on_query(len(self.calls))
        dim = "×".join(qa.group_by)
        rows_in = self.members(next(iter(self.scenario))) if not qa.group_by else self.members(dim)
        ratio = len(self.names) == 2
        cols = list(qa.group_by)
        for n in self.names:
            cols += [n, f"{n}·对比期", f"{n}·变化", f"{n}·变化率"] if qa.compare else [n]
        rows = []
        if not qa.group_by:  # 总量
            prev = [sum((m[i] or D(0) for m in rows_in), D(0)) for i in ((1,) if not ratio else (1, 2))]
            cur = [sum((m[i] or D(0) for m in rows_in), D(0)) for i in ((2,) if not ratio else (3, 4))]
            if self.totals:
                prev, cur = self.totals
            row = []
            for n, p, c in zip(self.names, prev, cur):
                row += [str(c), str(p), str(c - p), None]
            rows = [row]
        else:
            for m in rows_in:
                key = m[0].split(" / ") if "×" in dim else [m[0]]
                pairs = [(m[1], m[2])] if not ratio else [(m[1], m[3]), (m[2], m[4])]  # (prev, cur) 每个指标
                if qa.compare:
                    if all(c is None for _, c in pairs):  # 只在对比期出现的成员：带对比期的查询里没有
                        continue
                    row = list(key)
                    for p, c in pairs:
                        row += [None if c is None else str(c), None if p is None else str(p), None, None]
                else:
                    if all(p is None for p, _ in pairs):
                        continue
                    row = list(key) + [None if p is None else str(p) for p, _ in pairs]
                rows.append(row)
        ds = box._dataset("governed", f"替身 {dim or '总量'}", cols, rows, truncated=dim in self.truncated,
                          meta={"units": {}})
        return box._view(f"D{ds.seq}", ds), ds, "ok"


def additive_box(monkeypatch, scenario, budget=None, **fake):
    metric = SimpleNamespace(id="m1", name="指标甲", domain_id="dA")
    version = SimpleNamespace(id="v1", kind="base", expression="SUM(amount)", unit="件", unit_scale=1,
                              dimensions=[{"name": n} for n in scenario if "×" not in n], components=None)
    box = make_box(budget=budget, monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    monkeypatch.setattr(box, "_metric_sources", lambda code: (metric, version, [version], {}))
    queries = FakeQueries(box, ["指标甲"], scenario, **fake)
    monkeypatch.setattr(box, "_run_query", queries)
    return box, queries


SCENARIO = {
    "维度A": [("a1", D(500), D(200)), ("a2", D(300), D(310)), ("a3", D(200), D(190))],
    "维度B": [("b1", D(400), D(300)), ("b2", D(600), D(400))],
}


def test_explain_change_additive_ranks_dimensions_and_registers_units(monkeypatch):
    box, queries = additive_box(monkeypatch, SCENARIO)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    ds = box.datasets()[out["dataset"]]
    summary = ds.meta["summary"]
    assert summary["total"] == {"previous": "1000", "current": "700", "change": "-300"}  # 负总差额
    assert summary["verdict"] == "ranked_reasons" and summary["primary_dimension"] == "维度A"
    assert summary["partial"] is False and summary["scanned"] == ["维度A", "维度B"]
    first = dict(zip(ds.columns, ds.rows[0]))
    assert first["dimension"] == "维度A" and first["member"] == "a1" and first["ep"] == "1.0000" and first["role"] == "原因"
    assert summary["threshold_met"] is True and summary["cumulative_ep"] == "1.0000"
    js = {d["dimension"]: d["js"] for d in summary["dimensions"]}
    assert js["维度A"] > js["维度B"]
    assert ds.meta["units"]["contribution"] == {"kind": "delta", "unit": "件"}
    assert ds.meta["summary_units"]["total.change"] == {"kind": "delta", "unit": "件"}
    assert ds.meta["summary_units"]["cumulative_ep"] == {"kind": "share"}
    assert ds.inputs == ["D1", "D2", "D3"] and ds.meta["closure"] == ["D1", "D2", "D3"]  # 输入血缘
    assert box.budget.used_queries == 3 and queries.calls[0] == ((), "custom")


def test_explain_change_finds_exited_members_and_reconciles(monkeypatch):
    # 退出与新增相互抵消：不排名，增加项、减少项全部列出（便于核对新增 / 退出的标注）
    scenario = {"维度A": [("a1", D(500), D(505)), ("gone", D(300), None), ("new", None, D(290))]}
    box, queries = additive_box(monkeypatch, scenario)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x", "top_k": 20}))
    ds = box.datasets()[out["dataset"]]
    rows = {r[1]: dict(zip(ds.columns, r)) for r in ds.rows}
    assert rows["gone"]["change_type"] == "退出" and rows["gone"]["current"] == "0"
    assert rows["new"]["change_type"] == "新增" and rows["new"]["previous"] == "0"
    assert queries.calls[2] == (("维度A",), None)  # 补查对比期成员
    bad, _ = additive_box(monkeypatch, scenario, totals=([D(999)], [D(800)]))
    out = json.loads(bad.dispatch("explain_change", {"metric_code": "x"}))
    summary = bad.datasets()[out["dataset"]].meta["summary"]
    assert summary["verdict"] == "insufficient_evidence" and summary["partial"] is True
    assert "成员合计与总量对不上" in summary["unscanned"][0]["reason"]


def test_explain_change_offsetting_and_no_material_change(monkeypatch):
    box, _ = additive_box(monkeypatch, {"维度A": [("a1", D(100), D(200)), ("a2", D(200), D(105))]})
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    summary = box.datasets()[out["dataset"]].meta["summary"]
    assert summary["verdict"] == "offsetting" and summary["primary_dimension"] is None
    box, _ = additive_box(monkeypatch, {"维度A": [("a1", D(1000), D(1002)), ("a2", D(1000), D(1001))]})
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    summary = box.datasets()[out["dataset"]].meta["summary"]
    assert summary["verdict"] == "no_material_change" and summary["cumulative_ep"] is None


def test_explain_change_filters_apply_to_every_subquery_and_key_the_verdict(monkeypatch):
    """第 7 点第 9 条：筛选与 query_metric 相同，总量与各维度子查询都带上；被筛选的维度不再扫描；
    判定按（指标、规范化的筛选、窗口）登记，与对整个指标的判定分开。"""
    box, queries = additive_box(monkeypatch, SCENARIO)
    seen = []
    original = queries.__call__

    def record(qa):
        seen.append([f.model_dump() for f in qa.filters])
        return original(qa)
    monkeypatch.setattr(box, "_run_query", record)
    region = [{"dimension": "维度B", "values": ["b2", "b1", "b1"]}]
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x", "filters": region}))
    summary = box.datasets()[out["dataset"]].meta["summary"]
    assert seen and all(f == region for f in seen)  # 每个子查询都带同样的筛选
    assert summary["scanned"] == ["维度A"] and summary["scope"]["filters"] == region  # 维度B 被筛选：不扫描
    filtered = scope_key("x", [{"dimension": "维度B", "values": ["b1", "b2"]}], summary["scope"])
    assert list(box.explanations) == [filtered]
    json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    assert set(box.explanations) == {filtered, scope_key("x", [], summary["scope"])}  # 整个指标单独登记


def test_explain_change_with_every_dimension_filtered_judges_the_scope_total(monkeypatch):
    """筛选覆盖了全部维度时没有可扫描的维度：按范围总量判定（变化达到阈值为 ranked_reasons），不是证据不足。"""
    box, _ = additive_box(monkeypatch, SCENARIO)
    both = [{"dimension": "维度A", "values": ["a1"]}, {"dimension": "维度B", "values": ["b1"]}]
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x", "filters": both}))
    summary = box.datasets()[out["dataset"]].meta["summary"]
    assert summary["scanned"] == [] and summary["conclusion"] == "ranked_reasons" and summary["partial"] is False


def test_explain_change_partial_on_budget_truncation_and_deadline(monkeypatch):
    box, _ = additive_box(monkeypatch, SCENARIO, budget=Budget(queries=2))
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    ds = box.datasets()[out["dataset"]]
    summary = ds.meta["summary"]
    assert summary["partial"] is True and summary["scanned"] == ["维度A"]  # 预算耗尽：只扫了一个维度
    assert summary["unscanned"] == [{"dimension": "维度B", "reason": "查询额度已用完（2 次）"}]
    assert ds.meta["partial"] is True and "排名只在已扫描的维度内成立" in summary["note"]
    box, _ = additive_box(monkeypatch, SCENARIO, truncated=["维度A"])
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    summary = box.datasets()[out["dataset"]].meta["summary"]
    assert summary["unscanned"][0] == {"dimension": "维度A", "reason": "结果被截断（成员过多），不是全集"}
    assert summary["primary_dimension"] == "维度B"
    box, _ = additive_box(monkeypatch, SCENARIO)
    monkeypatch.setattr(box.budget, "remaining", lambda: 5)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    assert "总量查询没有完成（接近统一截止时间）" in out["error"]


def test_explain_change_revoked_midway_stops(monkeypatch):
    def revoke(n):
        if n == 1:
            monkeypatch.setattr(tools.access, "roles_of", lambda s, u: set())
    box, queries = additive_box(monkeypatch, SCENARIO, on_query=revoke)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    assert out["status"] == "denied" and "没有查询角色" in out["error"] and box.permission_lost
    assert len(queries.calls) == 1  # 撤权后不再发子查询


def test_explain_change_permission_denied_on_closure(monkeypatch):
    box, queries = additive_box(monkeypatch, SCENARIO)
    calls = {"n": 0}

    def sources(code):
        calls["n"] += 1
        if calls["n"] > 2:
            box._deny("来源资产当前无权访问", metric_code=code)
        return SimpleNamespace(name="指标甲"), SimpleNamespace(kind="base", expression="SUM(x)", unit=None,
                                                             unit_scale=1, dimensions=[{"name": "维度A"},
                                                                                       {"name": "维度B"}],
                                                             components=None), [], {}
    monkeypatch.setattr(box, "_metric_sources", sources)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    assert out["status"] == "denied" and box.denials[-1]["message"] == "来源资产当前无权访问"  # 按 ADR 0007 暂停任务


def test_explain_change_rejects_non_additive_and_unknown_dimensions(monkeypatch):
    box, queries = additive_box(monkeypatch, SCENARIO)
    version = SimpleNamespace(kind="base", expression="COUNT(DISTINCT user_id)", dimensions=[{"name": "维度A"}])
    monkeypatch.setattr(box, "_metric_sources", lambda code: (SimpleNamespace(name="指标乙"), version, [version], {}))
    out = json.loads(box.dispatch("explain_change", {"metric_code": "y"}))
    assert out["supported"] is False and "不可加" in out["reason"] and not queries.calls
    box, _ = additive_box(monkeypatch, SCENARIO)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x", "dimensions": ["维度Z"]}))
    assert "不是指标 x 允许的维度" in out["error"]
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x", "top_k": 21}))
    assert "ValidationError" in json.dumps(out, ensure_ascii=False) or "error" in out
    box, _ = additive_box(monkeypatch, SCENARIO)
    box.run.window = {}
    assert "需要本期与对比期" in json.loads(box.dispatch("explain_change", {"metric_code": "x"}))["error"]


def test_explain_change_depth_two_combines_top_dimensions(monkeypatch):
    scenario = {**SCENARIO,
                "维度A×维度B": [("a1 / b1", D(300), D(100)), ("a1 / b2", D(200), D(100)), ("a2 / b2", D(300), D(310)),
                              ("a3 / b1", D(100), D(200)), ("a3 / b2", D(100), D(-10))]}
    box, queries = additive_box(monkeypatch, scenario)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x", "depth": 2}))
    summary = box.datasets()[out["dataset"]].meta["summary"]
    assert "维度A×维度B" in summary["scanned"]
    assert summary["scanned"] == ["维度A", "维度B", "维度A×维度B"] and (("维度A", "维度B"), "custom") in queries.calls
    combo = next(d for d in summary["dimensions"] if d["dimension"] == "维度A×维度B")
    assert combo["js"] is None and summary["dimensions"][-1] == combo  # 含负值：不计算分布变化强度，排最后


def test_explain_change_ratio_metric_uses_numerator_and_denominator(monkeypatch):
    metric = SimpleNamespace(id="mr", name="比率丙", domain_id="dA")
    num, den = SimpleNamespace(id="vn", kind="base", expression="SUM(a)"), SimpleNamespace(id="vd", kind="base",
                                                                                           expression="COUNT(*)")
    version = SimpleNamespace(id="vr", kind="derived", expression="num / den", unit="元", unit_scale=1,
                              dimensions=[{"name": "维度A"}],
                              components=[{"metric_code": "num", "metric_id": "mn", "version_id": "vn"},
                                          {"metric_code": "den", "metric_id": "md", "version_id": "vd"}])
    box = make_box(monkeypatch=monkeypatch, objects=[])
    box.session.store.update({(MetricVersion, "vn"): num, (MetricVersion, "vd"): den,
                              (Metric, "mn"): SimpleNamespace(name="分子"), (Metric, "md"): SimpleNamespace(name="分母")})
    box.run.window = dict(WINDOW)
    monkeypatch.setattr(box, "_metric_sources", lambda code: (metric, version, [num, den], {}))
    scenario = {"维度A": [("a", D(50), D(10), D(30), D(10)), ("b", None, None, D(60), D(20)),
                         ("c", D(40), D(10), None, None), ("z", D(0), D(0), D(0), D(0))]}
    queries = FakeQueries(box, ["分子", "分母"], scenario)
    monkeypatch.setattr(box, "_run_query", queries)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "r", "top_k": 20}))
    ds = box.datasets()[out["dataset"]]
    summary = ds.meta["summary"]
    assert summary["algorithm"] == "ratio" and summary["total"] == {"previous": "4.5000", "current": "3.0000",
                                                                     "change": "-1.5000"}
    rows = {r[1]: dict(zip(ds.columns, r)) for r in ds.rows}
    assert rows["a"]["structure_effect"] == "-0.8333" and rows["a"]["rate_effect"] == "-0.6667"
    assert rows["a"]["member_change"] == "-2.0000"  # 成员比率变化与对总体比率的贡献分两列
    assert rows["b"]["change_type"] == "新增" and rows["c"]["change_type"] == "退出"
    assert summary["residual"] == "0.0000" and "拆法不唯一" in summary["note"]
    assert ds.meta["units"]["previous"] == {"kind": "ratio", "unit": "元"}
    assert queries.calls[0] == ((), "custom") and "z" not in rows  # 分母为 0 的成员不可计算，不参与排名


# --- ADR 0009 第 7 点：结论类型由 explain_change 判定；分子属于已发布比率口径时自动给出因子拆解 ----------------------

def test_material_change_threshold_is_a_per_metric_setting(monkeypatch):
    """变化显著性阈值是产品参数（默认 1%），口径版本可以单独配置；判定写进 explanations，报告的结论类型据此核对。"""
    box, _ = additive_box(monkeypatch, SCENARIO)  # 总量 1000 → 700，变化 30%
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    summary = box.datasets()[out["dataset"]].meta["summary"]
    assert summary["conclusion"] == "ranked_reasons" and summary["material_change_ratio"] == "0.01"
    key = scope_key("x", [], summary["scope"])
    assert box.explanations == {key: "ranked_reasons"} and "scope 照抄 summary.scope" in out["conclusion_note"]
    assert summary["scope"] == {"metric_code": "x", "filters": [], "start_date": WINDOW["start"], "end_date": WINDOW["end"],
                                "compare_start": WINDOW["compare_start"], "compare_end": WINDOW["compare_end"]}
    assert box.datasets()[out["dataset"]].meta["summary_units"]["material_change_ratio"]["kind"] == "share"  # 阈值可引用
    strict, _ = additive_box(monkeypatch, SCENARIO)
    strict._metric_sources("x")[1].material_change_ratio = D("0.5")  # 这个指标 50% 以内都算不显著
    out = json.loads(strict.dispatch("explain_change", {"metric_code": "x"}))
    summary = strict.datasets()[out["dataset"]].meta["summary"]
    assert summary["verdict"] == summary["conclusion"] == "no_material_change" and \
        strict.explanations[scope_key("x", [], summary["scope"])] == summary["verdict"]


def test_numerator_of_a_published_ratio_gets_lmdi_factors(monkeypatch):
    """所问指标 x 是已发布比率口径 r = x / y 的分子：explain_change 同时给出 x = y × r 的 LMDI 拆解，因子带口径编码。"""
    from avelis.models import Metric, MetricVersion

    box, queries = additive_box(monkeypatch, SCENARIO)
    ratio = MetricVersion(id="vr", metric_id="mr", kind="derived", status="published", expression="x / y")
    box.session.store[(Metric, "mr")] = SimpleNamespace(id="mr", code="r", name="比率甲")
    monkeypatch.setattr(box.session, "scalars", lambda stmt: [ratio])
    monkeypatch.setattr(box, "_metric", lambda code: (SimpleNamespace(name={"r": "比率甲", "y": "指标乙"}[code]), None))
    run_query = box._run_query

    def with_denominator(qa):
        if qa.metric_codes == ["y"]:  # 分母：两期 50 → 70
            box.budget.used_queries += 1
            return {}, box._dataset("governed", "指标乙", ["指标乙", "指标乙·对比期"], [["70", "50"]],
                                    meta={"metric_codes": ["y"]}), "ok"
        return run_query(qa)
    monkeypatch.setattr(box, "_run_query", with_denominator)
    out = json.loads(box.dispatch("explain_change", {"metric_code": "x"}))
    factor = box.datasets()[out["dataset"]].meta["summary"]["factor_decomposition"]
    assert factor["ratio_metric"] == "r" and factor["factors"] == ["y", "r"]
    ds = box.datasets()[factor["dataset"]]
    rows = {r[0]: dict(zip(ds.columns, r)) for r in ds.rows}
    assert set(rows) == {"y", "r"} and rows["y"]["factor"] == "指标乙" and rows["r"]["factor"] == "比率甲"
    total = sum(D(str(r["contribution"])) for r in rows.values())
    assert total == D("-300")  # 各因子贡献相加等于总差额
    assert D(str(rows["y"]["contribution"])) > 0 > D(str(rows["r"]["contribution"]))  # 分母上升、比率下降
    assert ds.meta["metric_codes"] == ["r", "x", "y"] and ds.meta["units"]["contribution"]["kind"] == "delta"
