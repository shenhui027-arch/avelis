"""P1 H：用中性假数据验证分布特征、证据修订与默认关闭，不连接数据库或模型。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from avelis import config
from avelis.analysis import agent, investigation, tools
from avelis.analysis.tools import Scope
from avelis.ask import service as ask_service
from avelis.models import Datasource, MetricVersion
from tests.unit.test_analysis_toolbox import governed, make_box
from tests.unit.test_business_neutral import PATTERN

WINDOW = {"start": "2026-09-01", "end": "2026-09-03", "compare_start": "2026-08-29", "compare_end": "2026-08-31"}
PROMPT_SHA256 = "0dc066b43893dc70a9276ce10f6694f318ea1f83df637224cc1b7c5b7baef066"
CLOSURE = {"cat.db.base_a"}
FEEDBACK = "变化在成员之间分布均匀；请用已有的记录类数据集 D7 检验驱动因素。"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """所有设置只来自显式字典，避免测试读取本机配置。"""
    value = config.build_settings({"AVELIS_SECRET": "unit-test-secret-only"})
    monkeypatch.setattr(config, "get_settings", lambda: value)
    monkeypatch.setattr(tools, "get_settings", lambda: value)
    monkeypatch.setattr(agent, "get_settings", lambda: value)
    return value


def settings(monkeypatch, enabled):
    value = config.build_settings(
        {"AVELIS_SECRET": "unit-test-secret-only", "AVELIS_ANALYSIS_INVESTIGATION": str(enabled).lower()}
    )
    monkeypatch.setattr(config, "get_settings", lambda: value)
    monkeypatch.setattr(tools, "get_settings", lambda: value)
    monkeypatch.setattr(agent, "get_settings", lambda: value)
    return value


def dataset(columns, rows, *, kind="calc", meta=None, inputs=(), truncated=False):
    return SimpleNamespace(
        kind=kind,
        columns=columns,
        rows=rows,
        meta=meta or {},
        inputs=list(inputs),
        truncated=truncated,
        seq=1,
        title="中性数据",
    )


def attribution_dataset(values=((300, 330), (200, 220), (100, 110)), *, contributions=True, truncated=False):
    columns = ["dimension", "member", "previous", "current", "member_change"]
    if contributions:
        columns.append("contribution")
    rows = [
        [
            "维度甲",
            f"成员{index}",
            previous,
            current,
            current - previous,
            *([current - previous] if contributions else []),
        ]
        for index, (previous, current) in enumerate(values)
    ]
    previous, current = sum(p for p, _ in values), sum(c for _, c in values)
    return dataset(
        columns,
        rows,
        truncated=truncated,
        meta={
            "summary": {
                "algorithm": "additive",
                "total": {"previous": str(previous), "current": str(current), "change": str(current - previous)},
            }
        },
    )


def trend_dataset(values=(100, 100, 100, 100, 100, 160), *, truncated=False):
    days = ["2026-08-29", "2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03"]
    return dataset(
        ["period", "指标 X"],
        [[day, value] for day, value in zip(days, values)],
        kind="governed",
        truncated=truncated,
        meta={"time_column": "period", "intent": {"metric_codes": ["metric_a"], "time_grain": "day"}},
    )


def event_dataset(
    *, sql="SELECT record_date, value_a FROM cat.db.records_c", kind="explore", value="2026-09-02", dialect=None
):
    meta = {"sql": sql}
    if dialect:
        meta["dialect"] = dialect
    return dataset(["record_date", "value_a"], [[value, "成员甲"]], kind=kind, meta=meta)


def report(*, cause_type="member", direction="up"):
    return {
        "title": "中性报告",
        "summary": "已有贡献定位",
        "conclusion": "ranked_reasons",
        "claims": [],
        "causes": [
            {
                "type": cause_type,
                "direction": direction,
                "entity": {"metric_code": "metric_a", "dimension": "维度甲", "member": "成员甲"},
                "evidence": ["D1"],
            }
        ],
    }


def uniform_distribution(*, change=60):
    return {
        "total_change": change,
        "dimensions": {"维度甲": {"members": 3, "top1_share": 0.5, "top3_share": 1.0, "uniform": True, "reason": None}},
        "time": {"reason": "没有日趋势"},
    }


@pytest.mark.parametrize("contributions", [True, False])
def test_distribution_ranks_absolute_contribution_or_member_change(contributions):
    value = investigation.distribution(
        attribution_dataset(((1000, 910), (1000, 995), (1000, 995)), contributions=contributions), None
    )
    dim = value["dimensions"]["维度甲"]
    assert value["total_change"] == -100
    assert dim["members"] == 3 and dim["top1_share"] == pytest.approx(0.9)
    assert dim["top3_share"] == pytest.approx(1.0) and dim["uniform"] is False


def test_distribution_uniform_rates_use_system_tolerance():
    dim = investigation.distribution(attribution_dataset(), None)["dimensions"]["维度甲"]
    assert investigation.get_settings().analysis_uniform_tolerance == 0.5
    assert investigation.get_settings().analysis_spike_share == 0.5
    assert dim["members"] == 3 and dim["uniform"] is True
    assert dim["top1_share"] == pytest.approx(0.5) and dim["top3_share"] == pytest.approx(1)
    nonuniform = investigation.distribution(attribution_dataset(((100, 110), (100, 110), (100, 140))), None)
    assert nonuniform["dimensions"]["维度甲"]["uniform"] is False


@pytest.mark.parametrize("values", [((100, 110), (200, 220)), ((0, 0), (100, 110), (200, 220))])
def test_distribution_does_not_call_fewer_than_three_positive_members_uniform(values):
    dim = investigation.distribution(attribution_dataset(values), None)["dimensions"]["维度甲"]
    assert dim["uniform"] is not True and dim["reason"]


def test_distribution_detects_single_day_deviation_from_previous_daily_mean():
    value = investigation.distribution(attribution_dataset(), trend_dataset(), window=WINDOW)
    assert value["time"]["max_day_share"] == pytest.approx(1)
    assert value["time"]["date"] == "2026-09-03" and value["time"]["concentrated"] is True
    spread = investigation.distribution(
        attribution_dataset(), trend_dataset((100, 100, 100, 120, 120, 120)), window=WINDOW
    )
    assert spread["time"]["max_day_share"] == pytest.approx(1 / 3)
    assert spread["time"]["concentrated"] is False


def test_distribution_missing_zero_change_and_truncated_inputs_have_reasons():
    missing = investigation.distribution(None, None)
    assert missing["total_change"] is None and missing["dimensions"] == {} and missing["time"]["reason"]
    zero = investigation.distribution(attribution_dataset(((100, 100),) * 3), None)
    assert zero["dimensions"]["维度甲"]["top1_share"] is None
    assert zero["dimensions"]["维度甲"]["reason"]
    partial = investigation.distribution(
        attribution_dataset(truncated=True), trend_dataset(truncated=True), window=WINDOW
    )
    assert partial["dimensions"]["维度甲"]["uniform"] is None
    assert partial["dimensions"]["维度甲"]["reason"] and partial["time"]["reason"]
    assert partial["time"]["max_day_share"] is None


def test_distribution_reconstructs_all_members_from_complete_governed_inputs():
    total = dataset(
        ["指标 X", "指标 X·对比期"], [[660, 600]], kind="governed", meta={"intent": {"metric_codes": ["metric_a"]}}
    )
    members = dataset(
        ["维度甲", "指标 X", "指标 X·对比期"],
        [["成员甲", 330, 300], ["成员乙", 220, 200], ["成员丙", 110, 100]],
        kind="governed",
        meta={"intent": {"group_by": ["维度甲"], "metric_codes": ["metric_a"], "compare": "custom"}},
    )
    selected = attribution_dataset()
    selected.rows = selected.rows[:1]
    selected.inputs = ["D1", "D2"]
    selected.meta["summary"]["scanned"] = ["维度甲"]
    value = investigation.distribution(selected, None, {"D1": total, "D2": members})
    assert value["dimensions"]["维度甲"]["members"] == 3
    assert value["dimensions"]["维度甲"]["uniform"] is True
    unavailable = investigation.distribution(selected, None, {"D1": total})
    assert unavailable["dimensions"]["维度甲"]["uniform"] is None
    assert unavailable["dimensions"]["维度甲"]["reason"]


def test_distribution_preserves_small_ratio_member_rates_before_display_rounding():
    intent = {"metric_codes": ["metric_a", "metric_b"], "compare": "custom"}
    columns = ["指标 X", "指标 X·对比期", "指标 Y", "指标 Y·对比期"]
    total = dataset(columns, [["3.3", "3", "30000", "30000"]], kind="governed", meta={"intent": intent})
    members = dataset(
        ["维度甲", *columns],
        [[name, "1.1", "1", "10000", "10000"] for name in ("成员甲", "成员乙", "成员丙")],
        kind="governed",
        meta={"intent": {**intent, "group_by": ["维度甲"]}},
    )
    selected = dataset(
        ["dimension", "member", "previous", "current", "contribution"],
        [["维度甲", "成员甲", "0.0001", "0.0001", "0"]],
        inputs=["D1", "D2"],
        meta={"summary": {"algorithm": "ratio", "scanned": ["维度甲"]}},
    )
    value = investigation.distribution(selected, None, {"D1": total, "D2": members})
    assert value["total_change"] == pytest.approx(0.00001)
    assert value["dimensions"]["维度甲"]["uniform"] is True


@pytest.mark.parametrize(("new_value", "new_base"), [(11, 100), (0, 0)])
def test_ratio_new_or_zero_base_members_do_not_block_uniform_positive_members(new_value, new_base):
    intent = {"metric_codes": ["metric_a", "metric_b"], "compare": "custom"}
    columns = ["指标 X", "指标 X·对比期", "指标 Y", "指标 Y·对比期"]
    total = dataset(columns, [[330 + new_value, 300, 3000 + new_base, 3000]], kind="governed", meta={"intent": intent})
    members = dataset(
        ["维度甲", *columns],
        [[name, 110, 100, 1000, 1000] for name in ("成员甲", "成员乙", "成员丙")]
        + [["成员丁", new_value, None, new_base, None]],
        kind="governed",
        meta={"intent": {**intent, "group_by": ["维度甲"]}},
    )
    selected = dataset([], [], inputs=["D1", "D2"], meta={"summary": {"algorithm": "ratio", "scanned": ["维度甲"]}})
    dim = investigation.distribution(selected, None, {"D1": total, "D2": members})["dimensions"]["维度甲"]
    assert dim["members"] == 4 and dim["uniform"] is True
    if not new_base:
        assert dim["top1_share"] is None and "集中度" in dim["reason"]


def test_distribution_block_is_short_and_describes_rates_not_causes():
    value = uniform_distribution()
    block = investigation.distribution_block(value)
    assert block.startswith("变化的分布特征") and "维度甲" in block and "均匀" in block
    assert len(block) <= 600
    value["dimensions"].update({f"维度{index}": dict(value["dimensions"]["维度甲"]) for index in range(100)})
    assert len(investigation.distribution_block(value)) <= 600
    assert len(investigation.distribution_block(value, limit=150)) <= 150
    missing = investigation.distribution(None, None)
    assert "不可计算" in investigation.distribution_block(missing)


@pytest.mark.parametrize("value", ["2026-09-02", "2026-09-02T12:30:00", date(2026, 9, 2), datetime(2026, 9, 2, 12, 30)])
def test_event_datasets_accept_date_values_and_exclude_nondates(value):
    assert investigation.event_datasets({"D7": event_dataset(value=value)}, CLOSURE) == []
    assert investigation.event_datasets({"D7": event_dataset(value="成员甲")}, CLOSURE) == []


def test_event_datasets_accept_explicit_date_column_type_without_rows():
    value = event_dataset(value=None)
    value.rows = []
    value.meta["units"] = {"record_date": {"kind": "date"}}
    assert investigation.event_datasets({"D7": value}, CLOSURE) == []


@pytest.mark.parametrize("kind", ["explore", "context", "injected"])
def test_event_datasets_include_existing_precheck_or_injected_records(kind):
    assert investigation.event_datasets({"D7": event_dataset(kind=kind)}, CLOSURE) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM cat.db.base_a",
        "SELECT * FROM db.base_a",
        "SELECT * FROM cat.db.records_c JOIN cat.db.base_a USING (key_a)",
        "SELECT 1",
        "SELECT (",
        "",
    ],
)
def test_event_datasets_require_every_sql_table_outside_closure(sql):
    assert investigation.event_datasets({"D7": event_dataset(sql=sql)}, CLOSURE) == []


def test_event_datasets_parse_cte_and_pass_datasource_dialect_explicitly(monkeypatch):
    original = investigation.sqlglot.parse
    calls = []

    def parse(sql, **kwargs):
        calls.append(kwargs.get("read"))
        return original(sql, **kwargs)

    monkeypatch.setattr(investigation.sqlglot, "parse", parse)
    sql = "WITH base_a AS (SELECT * FROM cat.db.records_c) SELECT * FROM base_a"
    assert investigation.event_datasets({"D7": event_dataset(sql=sql, dialect="postgres")}, CLOSURE) == ["D7"]
    assert calls == ["postgres"]
    calls.clear()
    assert investigation.event_datasets({"D7": event_dataset()}, CLOSURE) == []
    assert calls == []
    nested = "WITH records_c AS (SELECT * FROM cat.db.base_a) SELECT * FROM records_c"
    assert investigation.event_datasets({"D7": event_dataset(sql=nested)}, CLOSURE) == []


@pytest.mark.parametrize("cause_type", ["member", "factor"])
def test_revision_requires_uniform_member_or_factor_and_unused_event_dataset(cause_type):
    feedback = investigation.needs_revision(
        report(cause_type=cause_type), {"D7": event_dataset()}, CLOSURE, uniform_distribution()
    )
    assert feedback is None


@pytest.mark.parametrize("missing", ["cause", "uniform", "event"])
def test_revision_does_not_trigger_when_any_condition_is_missing(missing):
    value, datasets, distribution = report(), {"D7": event_dataset()}, uniform_distribution()
    if missing == "cause":
        value["causes"][0]["type"] = "calendar"
    elif missing == "uniform":
        distribution["dimensions"]["维度甲"]["uniform"] = False
    else:
        datasets = {}
    assert investigation.needs_revision(value, datasets, CLOSURE, distribution) is None


def test_revision_does_not_trigger_for_primary_event_evidence_row():
    value = report()
    value["causes"][0]["entity"] = {"evidence_id": "D7#1"}
    assert (
        investigation.needs_revision(
            value, {"D7": event_dataset(), "D8": event_dataset()}, CLOSURE, uniform_distribution()
        )
        is None
    )
    value["causes"][0]["entity"] = {"evidence_id": "D1#1"}
    assert investigation.needs_revision(value, {"D7": event_dataset()}, CLOSURE, uniform_distribution()) is None


@pytest.mark.parametrize(
    "reference",
    [
        {"causes": [{"type": "calendar", "entity": {}, "evidence": ["D7"]}]},
        {"claims": [{"parts": [{"dataset": "D7", "column": "value_a", "row": 1}]}]},
        {"claims": [{"refs": ["D7"]}]},
        {"fields": {"summary": {"parts": [{"dataset": "D7", "label": "value_a"}]}}},
        {"fields": {"title": {"segments": [{"ref": "D7", "kind": "value"}]}}},
        {"claims": [{"parts": [{"window": "dataset:D7"}]}]},
        {"causes": [{"type": "calendar", "entity": {"evidence_id": "D7#1"}, "evidence": ["D1"]}]},
    ],
)
def test_revision_treats_any_cause_claim_or_field_reference_as_used(reference):
    value = report()
    for key, addition in reference.items():
        if key == "causes":
            value[key] += addition
        else:
            value[key] = addition
    assert investigation.needs_revision(value, {"D7": event_dataset()}, CLOSURE, uniform_distribution()) is None


def test_revision_follows_saved_dependency_closure_and_lists_only_unused_records():
    value = report()
    value["claims"] = [{"parts": [{"dataset": "D8", "column": "value_a"}]}]
    derived = dataset(["value_a"], [[1]], meta={"closure": ["D7"]})
    datasets = {"D7": event_dataset(), "D8": derived}
    assert investigation.needs_revision(value, datasets, CLOSURE, uniform_distribution()) is None
    datasets["D9"] = event_dataset()
    feedback = investigation.needs_revision(value, datasets, CLOSURE, uniform_distribution())
    assert feedback is None


@pytest.mark.parametrize(("change", "direction"), [(60, "up"), (-60, "down")])
def test_offsetting_selects_the_matching_direction_among_first_two_causes(change, direction):
    value = report(cause_type="calendar", direction="down" if direction == "up" else "up")
    value["conclusion"] = "offsetting"
    value["causes"].append(report(direction=direction)["causes"][0])
    assert (
        investigation.needs_revision(value, {"D7": event_dataset()}, CLOSURE, uniform_distribution(change=change))
        is None
    )
    value["causes"][1]["type"] = "governance"
    assert (
        investigation.needs_revision(value, {"D7": event_dataset()}, CLOSURE, uniform_distribution(change=change))
        is None
    )
    value["causes"].append(report(direction=direction)["causes"][0])
    assert (
        investigation.needs_revision(value, {"D7": event_dataset()}, CLOSURE, uniform_distribution(change=change))
        is None
    )


def test_disabled_system_prompt_and_nonempty_context_pack_match_original_bytes(monkeypatch):
    assert (
        hashlib.sha256(agent.SYSTEM_PROMPT.encode()).hexdigest()
        == "d0c4e2bdc66105cefb4bc4c21de70f581796a807475f1007c42103b68cb0939c"
    )
    assert agent.system_message().content.encode() == agent.SYSTEM_PROMPT.format(**agent.PLACEHOLDER_EXAMPLE).encode()
    box = make_box(monkeypatch=monkeypatch)
    box.precheck_metric = {"code": "metric_a", "name": "指标 X", "kind": "base"}
    box.precheck_meta = [{"table": "cat.db.base_a", "columns": []}]
    box.precheck_notes = ["步骤甲未完成"]
    box.precheck_distribution = uniform_distribution()
    assert (
        box.context_pack().encode()
        == (
            '口径：{"code": "metric_a", "name": "指标 X", "kind": "base"}'
            '\n\n来源表 {"table": "cat.db.base_a", "columns": []}'
            "\n\n预检中没有完成的步骤：步骤甲未完成"
        ).encode()
    )


def test_enabled_context_pack_adds_distribution_after_attribution_and_respects_cap(monkeypatch):
    settings(monkeypatch, True)
    box = make_box(monkeypatch=monkeypatch)
    first = governed(box, ["指标 X"], [[1]])
    first.title = "归因占位"
    trend = governed(box, ["period", "指标 X"], [["2026-09-01", 1]])
    trend.title = "趋势占位"
    box.precheck_groups = {"attribution": ["D1"], "trend": ["D2"]}
    box.precheck_distribution = uniform_distribution()
    pack = box.context_pack()
    assert pack.index("D1 归因占位") < pack.index("变化的分布特征") < pack.index("D2 趋势占位")
    assert len(pack) <= box.PACK_CHARS
    box.PACK_CHARS = 500
    assert len(box.context_pack()) <= box.PACK_CHARS


@pytest.mark.parametrize("enabled", [False, True])
def test_change_precheck_computes_distribution_only_when_enabled(monkeypatch, enabled):
    settings(monkeypatch, enabled)
    box = make_box(monkeypatch=monkeypatch)
    box.run.window = dict(WINDOW)
    box.catalog = SimpleNamespace(versions={})
    monkeypatch.setattr(box, "describe_metric", lambda **kwargs: json.dumps({"code": "metric_a"}))
    monkeypatch.setattr(box, "changes", lambda **kwargs: "{}")
    monkeypatch.setattr(box, "data_health", lambda **kwargs: "{}")
    monkeypatch.setattr(box, "coverage", lambda **kwargs: "{}")
    monkeypatch.setattr(box, "_source_metadata", lambda code: [])
    attributed = box._dataset("calc", "归因占位", ["member"], [["成员甲"]])
    daily = governed(box, ["period", "指标 X"], [["2026-09-01", 1]])
    monkeypatch.setattr(box, "explain_change", lambda **kwargs: json.dumps({"dataset": f"D{attributed.seq}"}))
    monkeypatch.setattr(box, "query_metric", lambda **kwargs: json.dumps({"dataset": f"D{daily.seq}"}))
    calls = []

    def distribution(*args, **kwargs):
        calls.append((args, kwargs))
        return uniform_distribution()

    monkeypatch.setattr(investigation, "distribution", distribution)
    box.precheck(Scope(question_type="change", metric_codes=["metric_a"]))
    assert bool(calls) is enabled
    if enabled:
        assert calls[0][0][:2] == (attributed, daily)
        assert box.precheck_distribution == uniform_distribution()
        box.precheck(Scope(question_type="change", metric_codes=[]))
        assert box.precheck_distribution is None and len(calls) == 1
        assert "变化的分布特征" not in box.context_pack()


@pytest.mark.parametrize("ok", [False, True])
def test_disabled_verify_retains_original_phase_and_verification(monkeypatch, ok):
    box = make_box(monkeypatch=monkeypatch)
    box.report = report()
    result = {"ok": ok, "status": "verified" if ok else "failed", "claims": [], "fields": [], "causes": []}
    monkeypatch.setattr(agent, "ground_report", lambda *args: (args[0], deepcopy(result)))

    def unexpected(*args, **kwargs):
        raise AssertionError("关闭开关时不应执行 H 的反馈检查")

    monkeypatch.setattr(agent, "_investigation_feedback", unexpected)
    assert agent.verify({}, {"configurable": {"toolbox": box, "model": None}}) == {"phase": "done" if ok else "revise"}
    assert box.verification == {**result, "revised": False}


@pytest.mark.parametrize("revised", [False, True])
def test_enabled_verify_requests_at_most_one_h_revision_without_changing_grounding(monkeypatch, revised):
    settings(monkeypatch, True)
    box = make_box(monkeypatch=monkeypatch)
    box.report = report()
    result = {"ok": True, "status": "verified", "claims": [], "fields": [], "causes": []}
    monkeypatch.setattr(agent, "ground_report", lambda *args: (args[0], deepcopy(result)))
    monkeypatch.setattr(agent, "_investigation_feedback", lambda *args, **kwargs: FEEDBACK)
    assert agent.verify({"revised": revised}, {"configurable": {"toolbox": box, "model": None}}) == {
        "phase": "done" if revised else "revise"
    }
    assert box.verification == {**result, "revised": revised}
    if not revised:
        assert box.investigation_feedback == FEEDBACK


def test_investigation_feedback_uses_pinned_version_closure_and_actual_source_dialect(monkeypatch):
    settings(monkeypatch, True)
    current = MetricVersion(id="v-current", kind="base", components=None)
    pinned = MetricVersion(id="v-pinned", kind="base", components=None)
    source = Datasource(id="source-c", kind="fake", options={})
    cat = ask_service.Catalog([], {"metric_a": (SimpleNamespace(name="指标 X"), current)})
    box = make_box(monkeypatch=monkeypatch, catalog=cat, objects=[pinned, source])
    base = governed(box, ["指标 X"], [[1]])
    base.meta["versions"] = {"metric_a": "v-pinned"}
    records = box._dataset(
        "explore",
        "记录占位",
        ["record_date", "value_a"],
        [["2026-09-02", "成员甲"]],
        meta={"sql": "SELECT * FROM cat.db.records_c", "datasource_id": source.id, "dialect": "mysql"},
    )
    box.report = report()
    box.report["scope"] = {"metric_code": "metric_a"}
    box.precheck_distribution = uniform_distribution()
    versions, dialects = [], []

    def version_sources(session, version):
        versions.append(version)
        return {"base": SimpleNamespace(catalog_name="cat", db_name="db", name="base_a")}

    def dialect_of(datasource):
        assert datasource is source
        return SimpleNamespace(name="postgres")

    original = investigation.sqlglot.parse

    def parse(sql, **kwargs):
        dialects.append(kwargs["read"])
        return original(sql, **kwargs)

    def unexpected(_code):
        raise AssertionError("H 不应重走会修改权限或审计的工具路径")

    monkeypatch.setattr(agent.service, "version_sources", version_sources)
    monkeypatch.setattr(agent, "dialect_of", dialect_of)
    monkeypatch.setattr(investigation.sqlglot, "parse", parse)
    monkeypatch.setattr(box, "_metric_sources", unexpected)
    before_steps, before_budget = len(box.session.added), box.budget.view()
    before_budget["used"].pop("seconds")
    assert "D2" in agent._investigation_feedback(box)
    assert versions == [pinned] and dialects == ["postgres"]
    assert records.meta["dialect"] == "mysql"
    after_budget = box.budget.view()
    after_budget["used"].pop("seconds")
    assert len(box.session.added) == before_steps and after_budget == before_budget


def test_existing_verification_and_h_feedback_share_one_revision_and_one_tool_allowance(monkeypatch):
    settings(monkeypatch, True)
    box = make_box(monkeypatch=monkeypatch)
    box.precheck_distribution = uniform_distribution()
    initial_limit = box.budget.tool_calls
    seen = []

    def act(state, _config):
        seen.append(state["messages"])
        box.report = report()
        box.report["claims"] = [{"type": "fact", "text": "中性结论"}]
        return {"messages": [], "notes": []}

    result = {
        "ok": False,
        "status": "failed",
        "claims": [{"index": 0, "status": "unverified", "problems": ["既有引用问题"]}],
        "fields": [],
        "causes": [],
    }
    monkeypatch.setattr(agent, "act", act)
    monkeypatch.setattr(agent, "ground_report", lambda *args: (args[0], deepcopy(result)))
    monkeypatch.setattr(agent, "_investigation_feedback", lambda *args, **kwargs: FEEDBACK)
    value = agent.reason(
        {"question": "指标 X 为什么变化", "context": {}, "scope": None},
        {"configurable": {"toolbox": box, "model": None}},
    )
    assert value["revised"] is True and len(seen) == 2
    assert box.budget.tool_calls == initial_limit + agent.REVISION_TOOL_CALLS
    assert agent.REVISION_TOOL_CALLS == 6
    revision = seen[1][-1].content
    assert "既有引用问题" in revision and FEEDBACK in revision
    assert sum(FEEDBACK in message.content for message in seen[1]) == 1


def test_investigation_contrast_examples_are_short_neutral_and_opt_in(monkeypatch):
    examples = agent.INVESTIGATION_EXAMPLES
    assert len(examples) <= 800 and not PATTERN.search(examples)
    assert examples.count("反例") == 3 and examples.count("错在哪") == 3 and examples.count("正确写法") == 3
    assert "成员甲" in examples and "记录表丙" in examples and "证据不足" in examples
    baseline = agent.system_message().content
    assert examples not in baseline
    settings(monkeypatch, True)
    assert agent.system_message().content == baseline + examples
