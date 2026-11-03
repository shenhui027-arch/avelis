"""过拟合审查第三批：量纲、列契约与数字显示的离线回归，不连接数据库或模型。"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from reportlab.graphics.shapes import Circle, String

from avelis import config, configfile
from avelis.analysis import calc, dataset_access, dataset_projection, explore, export, grounding, measures
from avelis.analysis import service as analysis_service
from avelis.analysis.figures import dataset_charts, report_figures
from avelis.api import analysis as analysis_api
from avelis.api import ask as ask_api
from avelis.api import auth, schemas
from avelis.ask import service as ask_service
from avelis.ask.intent import Intent
from avelis.models import AnalysisDataset, Metric, MetricVersion
from tests.unit.test_analysis_explore import ok as validate_explore
from tests.unit.test_overfit_batch1 import _batch1_additive_derived_box, _batch1_box, _batch1_governed

NOW = datetime(2026, 3, 31, tzinfo=timezone.utc)
SECRET = "offline-unit-test-secret-0123456789"


def _version(**changes):
    return SimpleNamespace(
        **{
            "id": "version_a",
            "metric_id": "metric_id_a",
            "kind": "base",
            "expression": "SUM(value_a)",
            "value_kind": None,
            "unit": "单位甲",
            "unit_scale": 1,
            "polarity": "down_good",
            "dimensions": [{"name": "dimension_a", "label": "维度甲"}, {"name": "dimension_b"}],
            **changes,
        }
    )


def _metric():
    return SimpleNamespace(id="metric_id_a", code="metric_a", name="指标甲", current_version_id="latest_a")


class _Session:
    """只按传入对象取值；查询列表按 ORM 选择的实体分发，不执行 SQL。"""

    def __init__(self, version=None, datasets=()):
        version = version or _version()
        self.store = {(Metric, "metric_id_a"): _metric(), (MetricVersion, version.id): version}
        self.datasets = list(datasets)
        self.reads = []

    def get(self, entity, key, **kwargs):
        self.reads.append((entity, key))
        return self.store.get((entity, key))

    def scalars(self, stmt):
        return self.datasets if stmt.column_descriptions[0]["entity"] is AnalysisDataset else []


def _dataset(kind="governed", *, columns=None, meta=None, seq=1):
    return SimpleNamespace(
        id=f"dataset_{seq}",
        seq=seq,
        kind=kind,
        title="数据甲",
        columns=columns or ["dimension_a", "指标甲"],
        rows=[[3, 40]],
        truncated=False,
        inputs=[],
        answer_id=None,
        created_at=NOW,
        meta=meta
        if meta is not None
        else {
            "versions": {"metric_a": "version_a"},
            "metric_version_ids": ["version_a"],
            "intent": {"metric_codes": ["metric_a"], "group_by": ["dimension_a"]},
        },
    )


def _run(report=None):
    return SimpleNamespace(
        id="run_a",
        task_id=None,
        parent_run_id=None,
        conversation_id="run_a",
        confirmation_mode="auto",
        question="查看指标甲",
        trigger="manual",
        status="succeeded",
        window={},
        budget={},
        model_adapter="offline",
        reasoning_effort=None,
        model_choice=None,
        error=None,
        created_at=NOW,
        started_at=None,
        finished_at=None,
        checkpoint={},
        report=report or {"title": "报告甲"},
        verification={"ok": True},
        plan={},
    )


@pytest.mark.parametrize(
    "kind,expression,declared,expected",
    [
        ("base", "SUM(value_a)", "ratio", "ratio"),
        ("derived", "a / b", "level", "level"),
        ("derived", "a / b", "count", "count"),
        ("base", "SUM(value_a)", None, "level"),
        ("derived", "a / b", None, "ratio"),
        ("derived", "a + b", None, "level"),
    ],
)
def test_metric_value_kind_overrides_inference_and_preserves_default(kind, expression, declared, expected):
    version = _version(kind=kind, expression=expression, value_kind=declared, unit_scale=1000)
    assert measures.metric_kind(version) == expected
    assert measures.metric_unit(version) == {"kind": expected, "unit": "单位甲", "scale": "1000"}


@pytest.mark.parametrize("derived", [False, True])
def test_declared_ratio_rejects_member_sums_even_for_additive_expression(monkeypatch, derived):
    if derived:
        box = _batch1_additive_derived_box(monkeypatch, "a + b")
    else:
        box = _batch1_box(monkeypatch)
        _batch1_governed(box)
    monkeypatch.setattr(box, "_metric", lambda code: box.catalog.versions[code])
    version = box.catalog.versions["metric_a"][1]
    box._require_additive("D1", ["指标甲"])
    version.value_kind = "ratio"
    for column in ("指标甲", "指标甲·上一期", "指标甲·变化"):
        with pytest.raises(calc.CalcError, match="口径声明为比率量纲"):
            box._require_additive("D1", [column])


def test_declared_ratio_component_cannot_bypass_member_sum_guard(monkeypatch):
    box = _batch1_additive_derived_box(monkeypatch, "a + b", second_expression='SUM("指标乙")')
    monkeypatch.setattr(box, "_metric", lambda code: box.catalog.versions[code])
    box.session.get(MetricVersion, "组成版本甲").value_kind = "ratio"
    with pytest.raises(calc.CalcError, match="不能按成员相加"):
        box._require_additive("D1", ["指标甲"])


@pytest.mark.parametrize("retained", [False, True])
def test_calculated_results_inherit_only_explicitly_retained_time_column(monkeypatch, retained):
    box = _batch1_box(monkeypatch)
    source = _batch1_governed(box)
    source.meta["time_column"] = "维度甲"
    args = {"keys": ["维度甲"]} if retained else {}
    out = json.loads(
        box._calc(
            "aggregate",
            args,
            ["D1"],
            lambda: ("计算甲", ["维度甲", "value_a"], [["成员甲", 40]], {}, {"units": {"value_a": {"kind": "count"}}}),
        )
    )
    assert out["dataset"] == "D2"
    meta = box.datasets()["D2"].meta
    assert meta.get("time_column") == ("维度甲" if retained else None)
    assert dataset_projection.build_column_roles(out["columns"], meta)["维度甲"]["role"] == (
        "time" if retained else "dimension"
    )


def test_calendar_calculation_registers_period_and_derive_marks_mixed_values_unknown(monkeypatch):
    box = _batch1_box(monkeypatch)
    _batch1_governed(box)
    adjusted = json.loads(box.calendar_adjust("D1", "指标甲", "指标甲·上一期", {"维度甲": "成员甲"}))
    calendar_meta = box.datasets()[adjusted["dataset"]].meta
    assert calendar_meta["time_column"] == "period"
    assert dataset_projection.build_column_roles(adjusted["columns"], calendar_meta)["period"]["role"] == "time"
    derived = json.loads(
        box.derive(
            "指标乙",
            [
                {"name": "a", "dataset": "D1", "column": "指标甲", "match": {"维度甲": "成员甲"}},
                {"name": "b", "dataset": "D1", "column": "指标甲·上一期", "match": {"维度甲": "成员甲"}},
            ],
            "a / b",
        )
    )
    derived_meta = box.datasets()[derived["dataset"]].meta
    assert derived_meta["units"]["value"] == {"kind": "unknown"}
    assert derived_meta["summary_units"]["value"] == {"kind": "ratio"}
    assert "value" not in dataset_projection.build_column_roles(derived["columns"], derived_meta)


@pytest.mark.parametrize("constant", ["1e6", "1e9", "60", "24", "3600"])
@pytest.mark.parametrize("engine", ["postgres", "starrocks"])
def test_formula_and_exploration_share_conversion_constants(constant, engine):
    assert explore.ALLOWED_CONSTANTS is measures.CONSTANTS
    assert measures.constants(f"a / {constant}") == []
    assert measures.constants("a / 7") == [Decimal(7)]
    result = validate_explore(engine, f"SELECT SUM(qty) / {constant} AS measure_a FROM d1.facts")
    assert result.columns[0].eligible and not result.columns[0].assumption


@pytest.mark.parametrize(
    "sql,key",
    [
        ("SELECT qty AS key_a, SUM(amount) AS value_a FROM d1.facts GROUP BY qty", "key_a"),
        ("SELECT item_id, SUM(qty) AS value_a FROM d1.facts GROUP BY item_id", "item_id"),
        (
            "WITH grouped AS (SELECT qty AS numeric_key, SUM(amount) AS total FROM d1.facts GROUP BY qty) "
            "SELECT numeric_key AS key_a, total AS value_a FROM grouped",
            "key_a",
        ),
    ],
)
def test_exploration_numeric_group_keys_keep_dimension_role_through_aliases_and_ctes(sql, key):
    result = validate_explore("postgres", sql)
    assert result.group_by == [key]
    meta = {
        "units": {column.name: {"kind": column.kind} for column in result.columns},
        "intent": {"group_by": result.group_by},
    }
    roles = dataset_projection.build_column_roles([column.name for column in result.columns], meta)
    assert roles[key] == {"role": "dimension", "label": key}
    assert roles["value_a"]["role"] == "measure"


@pytest.mark.parametrize("direction", ["up_good", "down_good", None])
def test_governed_polarity_covers_comparison_and_change_columns_without_guessing(direction):
    columns = ["dimension_a", "指标甲", "指标甲·上一自然期", "指标甲·变化", "指标甲·变化率", "指标乙·变化"]
    polarity = measures.governed_polarity(columns, {"指标甲": _version(polarity=direction)}, "上一自然期")
    expected = {column: direction for column in columns[1:5]} if direction else {}
    assert polarity == expected


def test_column_roles_follow_registered_semantics_and_labels():
    columns = [
        "time_a",
        "dimension_a",
        "dimension_b",
        "其他键",
        "期间",
        "列·变化率",
        "count_a",
        "ratio_a",
        "share_a",
        "delta_a",
        "rate_a",
        "unknown_a",
    ]
    meta = {
        "time_column": "time_a",
        "intent": {"group_by": ["time_a", "dimension_a", "dimension_b"]},
        "dimensions": [{"name": "dimension_a", "label": "维度甲"}, {"name": "dimension_b"}],
        "measure_labels": {"列·变化率": "指标甲", "delta_a": "指标甲", "rate_a": "指标甲"},
        "units": {
            "time_a": {"kind": "count"},
            "dimension_a": {"kind": "count"},
            "列·变化率": {"kind": "level", "additive": True},
            "count_a": {"kind": "count"},
            "ratio_a": {"kind": "ratio"},
            "share_a": {"kind": "share"},
            "delta_a": {"kind": "delta", "additive": True},
            "rate_a": {"kind": "rate_change"},
            "unknown_a": {"kind": "unknown"},
        },
    }
    original = deepcopy(meta)
    roles = dataset_projection.build_column_roles(columns, meta)
    assert roles["time_a"] == {"role": "time", "label": "time_a", "grain": None}
    assert roles["dimension_a"] == {"role": "dimension", "label": "维度甲"}
    assert roles["dimension_b"] == {"role": "dimension", "label": "dimension_b"}
    assert roles["期间"]["role"] == "dimension"  # 没有 time_column，不凭名字猜时间列。
    assert roles["其他键"]["role"] == "dimension"
    assert roles["列·变化率"] == {"role": "measure", "label": "指标甲", "additive": True}
    assert all(roles[column]["role"] == "measure" for column in ("count_a", "ratio_a", "share_a"))
    assert roles["delta_a"] == {"role": "change", "label": "指标甲", "additive": True}
    assert roles["rate_a"] == {"role": "rate", "label": "指标甲", "additive": False}
    assert "unknown_a" not in roles and meta == original


def test_column_roles_do_not_invent_dimensions_when_units_are_missing():
    columns = ["time_a", "dimension_a", "dimension_b", "value_a", "期间"]
    meta = {
        "time_column": "time_a",
        "intent": {"group_by": ["dimension_a"]},
        "dimensions": [{"name": "dimension_b", "label": "维度乙"}],
    }
    assert dataset_projection.build_column_roles(columns, meta) == {
        "time_a": {"role": "time", "label": "time_a", "grain": None},
        "dimension_a": {"role": "dimension", "label": "dimension_a"},
        "dimension_b": {"role": "dimension", "label": "维度乙"},
    }


def test_registered_measure_role_wins_over_unused_dimension_with_same_name():
    version = _version(value_kind="count", dimensions=[{"name": "value", "label": "维度甲"}])
    roles = dataset_projection.build_column_roles(
        ["value"],
        {
            "units": {"value": {"kind": "count", "additive": True}},
            "dimensions": version.dimensions,
            "measure_labels": {"value": "指标甲"},
        },
    )
    assert roles["value"] == {"role": "measure", "label": "指标甲", "additive": True}
    result = ask_service._result_contract(
        {"columns": ["value"], "rows": [[40]]},
        Intent(metric_code="metric_a"),
        [(_metric(), version)],
        value_column="value",
    )
    assert result["column_roles"]["value"] == {"role": "measure", "label": "指标甲", "additive": True}


@pytest.mark.parametrize("source", ["versions", "metric_version_ids"])
def test_dataset_meta_reads_locked_version_and_does_not_mutate_saved_metadata(source):
    columns = ["dimension_a", "指标甲", "指标甲·上一自然期", "指标甲·变化", "指标甲·变化率"]
    meta = {
        source: {"metric_a": "version_a"} if source == "versions" else ["version_a"],
        "intent": {"compare": "previous_calendar"},
        "units": {"指标甲": {"kind": "level"}},
    }
    dataset = _dataset(columns=columns, meta=meta)
    session = _Session(_version(value_kind="ratio", unit=None))
    session.store[(MetricVersion, "latest_a")] = _version(id="latest_a", polarity="up_good", value_kind="count")
    original = deepcopy(meta)
    projected = dataset_projection.dataset_meta(session, dataset)
    assert projected["units"]["指标甲"]["kind"] == "ratio"
    assert projected["units"]["指标甲·变化"] == {"kind": "delta", "of_ratio": True, "additive": False}
    assert projected["polarity"] == {column: "down_good" for column in columns[1:]}
    assert projected["dimension_labels"] == {"dimension_a": "维度甲", "dimension_b": "dimension_b"}
    assert projected["measure_labels"] == {column: "指标甲" for column in columns[1:]}
    assert (MetricVersion, "latest_a") not in session.reads and meta == original


@pytest.mark.parametrize("kind", ["explore", "calc"])
def test_dataset_meta_does_not_query_versions_for_ungoverned_results(kind):
    dataset = _dataset(kind=kind, meta={"units": {"指标甲": {"kind": "count"}}})
    session = _Session()
    assert dataset_projection.dataset_meta(session, dataset) == dataset.meta
    assert session.reads == []


@pytest.mark.parametrize("source", ["versions", "metric_version_ids"])
def test_report_dimension_labels_use_asked_metric_locked_version(source):
    report = {
        "metric_code": "metric_a",
        "causes": [
            {"type": "member", "evidence": ["D1"], "entity": {"dimension": "dimension_a", "member": "成员甲"}},
            {"type": "calendar", "entity": {"dimension": "dimension_a"}},
            {"type": "member", "restricted": True, "entity": {}},
        ],
    }
    dataset = _dataset(meta={source: {"metric_a": "version_a"} if source == "versions" else ["version_a"]})
    session = _Session()
    session.store[(MetricVersion, "other_a")] = _version(
        id="other_a", dimensions=[{"name": "dimension_a", "label": "其他显示名"}]
    )
    unrelated = _dataset(seq=2, meta={"versions": {"metric_b": "other_a"}})
    original = deepcopy(report)
    projected = dataset_projection.report_dimension_labels(session, report, [dataset, unrelated])
    assert projected["causes"][0]["entity"]["dimension_label"] == "维度甲"
    assert "dimension_label" not in projected["causes"][1]["entity"]
    assert projected["causes"][2]["entity"] == {} and report == original


def test_report_dimension_label_conflict_uses_dimension_name():
    report = {
        "scope": {"metric_code": "metric_a"},
        "causes": [
            {"type": "member", "evidence": ["D1", "D2"], "entity": {"dimension": "dimension_a", "member": "成员甲"}},
        ],
    }
    session = _Session()
    session.store[(MetricVersion, "version_b")] = _version(
        id="version_b", dimensions=[{"name": "dimension_a", "label": "维度乙"}]
    )
    datasets = [_dataset(), _dataset(seq=2, meta={"versions": {"metric_a": "version_b"}})]
    projected = dataset_projection.report_dimension_labels(session, report, datasets)
    assert projected["causes"][0]["entity"]["dimension_label"] == "dimension_a"


@pytest.mark.parametrize("evidence_source", ["evidence", "contribution", "entity"])
def test_report_dimension_labels_follow_calculation_sources_and_ignore_unrelated_rerun(evidence_source):
    cause = {"type": "member", "entity": {"dimension": "dimension_a", "member": "成员甲"}}
    if evidence_source == "evidence":
        cause["evidence"] = ["D2"]
    elif evidence_source == "contribution":
        cause["contribution"] = {"segments": [{"kind": "value", "ref": "D2"}]}
    else:
        cause["entity"]["evidence_id"] = "D2#1"
    report = {"metric_code": "metric_a", "causes": [cause]}
    session = _Session()
    session.store[(MetricVersion, "version_b")] = _version(
        id="version_b", dimensions=[{"name": "dimension_a", "label": "维度乙"}]
    )
    calculation = _dataset("calc", seq=2, meta={})
    calculation.inputs = ["D1"]
    rerun = _dataset(seq=3, meta={"versions": {"metric_a": "version_b"}})
    projected = dataset_projection.report_dimension_labels(session, report, [_dataset(), calculation, rerun])
    assert projected["causes"][0]["entity"]["dimension_label"] == "维度甲"


@pytest.mark.parametrize("kind", ["governed", "explore", "calc"])
def test_dataset_detail_and_schema_keep_column_contract(kind):
    meta = {
        "time_column": "time_a",
        "intent": {"group_by": ["dimension_a"]},
        "units": {"指标甲": {"kind": "count", "additive": True}},
        "dimension_labels": {"dimension_a": "维度甲"},
        "measure_labels": {"指标甲": "指标甲"},
        "polarity": {"指标甲": "down_good"},
    }
    dataset = _dataset(kind=kind, columns=["time_a", "dimension_a", "指标甲"], meta=meta)
    dataset.rows = [[202603, 3, 40]]
    detail = analysis_api._dataset_detail(dataset, session=_Session())
    serialized = schemas.AnalysisDatasetDetailOut.model_validate(detail).model_dump(mode="json")
    assert serialized["column_roles"] == {
        "time_a": {"role": "time", "label": "time_a", "grain": None, "additive": None},
        "dimension_a": {"role": "dimension", "label": "维度甲", "grain": None, "additive": None},
        "指标甲": {"role": "measure", "label": "指标甲", "grain": None, "additive": True},
    }
    assert serialized["polarity"] == {"指标甲": "down_good"}
    assert serialized["rows"] == [[202603, 3, 40]]


def test_run_view_and_schema_expose_governed_explore_and_calculated_contracts():
    datasets = [
        _dataset(),
        _dataset("explore", seq=2, meta={"units": {"指标甲": {"kind": "level"}}}),
        _dataset("calc", seq=3, meta={"units": {"指标甲": {"kind": "delta"}}}),
    ]
    run = _run(
        {
            "metric_code": "metric_a",
            "causes": [
                {"type": "member", "evidence": ["D1"], "entity": {"dimension": "dimension_a", "member": "成员甲"}},
            ],
        }
    )
    view = analysis_service.run_view(_Session(datasets=datasets), run)
    serialized = schemas.AnalysisRunOut.model_validate(view).model_dump(mode="json")
    governed, exploratory, calculated = serialized["datasets"]
    assert governed["column_roles"]["dimension_a"]["label"] == "维度甲"
    assert governed["polarity"] == {"指标甲": "down_good"}
    assert exploratory["column_roles"]["指标甲"]["role"] == "measure"
    assert calculated["column_roles"]["指标甲"]["role"] == "change"
    assert view["report"]["causes"][0]["entity"]["dimension_label"] == "维度甲"
    assert "dimension_label" not in run.report["causes"][0]["entity"]


def test_run_view_restricted_dataset_does_not_leak_column_labels_or_polarity(monkeypatch):
    dataset = _dataset()
    monkeypatch.setattr(dataset_access, "can_read_dataset", lambda *args: False)
    view = analysis_service.run_view(_Session(datasets=[dataset]), _run(), user=SimpleNamespace(id="reader_a"))
    projected = view["datasets"][0]
    assert projected["restricted"] and projected["rows"] == []
    assert projected["units"] == projected["polarity"] == projected["column_roles"] == {}
    assert projected["charts"] == []


@pytest.mark.parametrize("time_grain", [None, "month"])
def test_ask_simple_result_contract_labels_numeric_dimensions_and_time(time_grain):
    columns = (["time_a"] if time_grain else []) + ["dimension_a", "value"]
    result = {"columns": columns, "rows": [[202603, 3, 40] if time_grain else [3, 40]]}
    original = deepcopy(result)
    intent = Intent(metric_code="metric_a", group_by=["dimension_a"], time_grain=time_grain)
    projected = ask_service._result_contract(
        result, intent, [(_metric(), _version(value_kind="count"))], value_column="value"
    )
    assert projected["units"]["value"] == {"kind": "count", "unit": "单位甲", "additive": True}
    assert projected["polarity"] == {"value": "down_good"}
    assert projected["column_roles"]["dimension_a"] == {"role": "dimension", "label": "维度甲"}
    assert projected["column_roles"]["value"] == {"role": "measure", "label": "指标甲", "additive": True}
    if time_grain:
        assert projected["column_roles"]["time_a"]["role"] == "time"
    assert projected["rows"] == result["rows"] and result == original


@pytest.mark.parametrize("direction", ["up_good", "down_good", None])
def test_ask_planned_result_contract_carries_change_roles_and_polarity(direction):
    columns = ["dimension_a", "指标甲", "指标甲·上一自然期", "指标甲·变化", "指标甲·变化率"]
    intent = Intent(metric_code="metric_a", group_by=["dimension_a"], compare="previous_calendar")
    projected = ask_service._result_contract(
        {"columns": columns, "rows": [[3, 40, 50, -10, -0.2]]},
        intent,
        [(_metric(), _version(value_kind="ratio", polarity=direction))],
    )
    assert [projected["column_roles"][column]["role"] for column in columns] == [
        "dimension",
        "measure",
        "measure",
        "change",
        "rate",
    ]
    assert all(projected["column_roles"][column]["label"] == "指标甲" for column in columns[1:])
    assert projected["polarity"] == ({column: direction for column in columns[1:]} if direction else {})


def test_no_comparison_metric_name_suffix_does_not_overwrite_another_metric_contract():
    first = SimpleNamespace(id="metric_id_b", code="metric_b", name="指标甲·变化率")
    columns = [first.name, "指标甲"]
    intent = Intent(metric_codes=["metric_b", "metric_a"])
    result = ask_service._result_contract(
        {"columns": columns, "rows": [[40, 0.2]]},
        intent,
        [
            (first, _version(value_kind="count", polarity="up_good")),
            (_metric(), _version(value_kind="ratio", polarity="down_good")),
        ],
    )
    assert result["units"][first.name]["kind"] == "count"
    assert result["units"]["指标甲"]["kind"] == "ratio"
    assert result["column_roles"] == {
        first.name: {"role": "measure", "label": first.name, "additive": True},
        "指标甲": {"role": "measure", "label": "指标甲", "additive": False},
    }
    assert result["polarity"] == {first.name: "up_good", "指标甲": "down_good"}


@pytest.mark.parametrize("scenario", ["simple", "planned", "inconsistent"])
def test_ask_fulfil_attaches_contract_without_mutating_shared_cached_result(monkeypatch, scenario):
    planned = scenario == "planned"
    intent = Intent(
        metric_code="metric_a",
        metric_codes=["metric_b"] if scenario == "inconsistent" else [],
        compare="previous_calendar" if planned else None,
    )
    shared = {"columns": ["指标甲"] if planned else ["value"], "rows": [[40]]}
    original = deepcopy(shared)
    answer = SimpleNamespace(status="pending", result={})

    def fulfil_from_cache(*args):
        answer.status, answer.result = "answered", shared

    monkeypatch.setattr(ask_service, "get_settings", lambda: object())
    monkeypatch.setattr(ask_service, "_fulfil_plan" if planned else "_fulfil", fulfil_from_cache)
    ask_service.fulfil(
        object(),
        object(),
        intent,
        answer,
        ask_service.Catalog([], {"metric_a": (_metric(), _version(value_kind="count"))}),
    )
    assert answer.result["column_roles"] and answer.result["polarity"]
    assert answer.result is not shared and shared == original
    if not planned:
        assert answer.result["column_roles"]["value"] == {"role": "measure", "label": "指标甲", "additive": True}
        assert answer.result["units"]["value"]["kind"] == "count"
        assert answer.result["polarity"] == {"value": "down_good"}
    if scenario == "inconsistent":
        historical = SimpleNamespace(
            status="answered", result=shared, intent=intent.model_dump(mode="json"), metric_version_ids=["version_a"]
        )
        projected = ask_api._answer_result(historical, _Session(_version(value_kind="count")))
        assert projected["column_roles"]["value"] == {"role": "measure", "label": "指标甲", "additive": True}
        assert projected["units"]["value"]["kind"] == "count"


def test_historical_answer_contract_uses_locked_version_without_mutating_saved_result():
    result = {"columns": ["dimension_a", "value"], "rows": [[3, 40]]}
    answer = SimpleNamespace(
        status="answered",
        result=result,
        intent=Intent(metric_code="metric_a", group_by=["dimension_a"]).model_dump(mode="json"),
        metric_version_ids=["version_a"],
    )
    session = _Session(_version(value_kind="count"))
    projected = ask_api._answer_result(answer, session)
    assert projected["column_roles"]["dimension_a"]["label"] == "维度甲"
    assert projected["units"]["value"]["kind"] == "count"
    assert projected["polarity"] == {"value": "down_good"}
    assert answer.result is result and "column_roles" not in result
    assert projected["charts"][0]["kind"] == "bar"
    assert "charts" not in result
    assert (MetricVersion, "latest_a") not in session.reads


@pytest.mark.parametrize("case", ["missing_version", "invalid_intent", "ambiguous_versions", "renamed_metric"])
def test_historical_answer_does_not_guess_when_metadata_cannot_be_resolved(case):
    result = {"columns": ["value"], "rows": [[40]]}
    answer = SimpleNamespace(
        status="answered",
        result=result,
        intent=Intent(metric_code="metric_a").model_dump(mode="json"),
        metric_version_ids=["version_a"],
    )
    session = _Session()
    if case == "missing_version":
        session.store.pop((MetricVersion, "version_a"))
    elif case == "invalid_intent":
        answer.intent["time_grain"] = "invalid"
    elif case == "ambiguous_versions":
        session.store[(MetricVersion, "version_b")] = _version(id="version_b")
        answer.metric_version_ids.append("version_b")
    else:
        answer.intent["compare"] = "previous_calendar"
        result["columns"] = ["旧指标甲"]
    assert ask_api._answer_result(answer, session) is result


@pytest.mark.parametrize("case", ["existing_contract", "blocked", "no_session"])
def test_historical_answer_preserves_existing_or_unavailable_contract(case):
    result = {"columns": ["value"], "rows": [[40]]}
    answer = SimpleNamespace(status="answered", result=result)
    if case == "existing_contract":
        result["column_roles"] = {}
    elif case == "blocked":
        answer.status = "blocked"
    session = _Session() if case != "no_session" else None
    projected = ask_api._answer_result(answer, session)
    if case == "existing_contract":
        assert projected == {**result, "charts": []} and "charts" not in result
    else:
        assert projected is result
    assert session is None or session.reads == []


def test_export_contract_uses_numeric_dimensions_and_rate_without_suffix():
    dataset = {
        "columns": ["dimension_a", "value_a"],
        "rows": [[101, 0.2], [102, 0.3]],
        "column_roles": {
            "dimension_a": {"role": "dimension", "label": "维度甲"},
            "value_a": {"role": "rate", "label": "指标甲"},
        },
    }
    # rate 不再单独成图，见 test_charts.py::test_empty_or_no_measure_and_unsupported_shapes。
    assert dataset_charts(dataset) == []
    dataset["column_roles"]["value_a"]["role"] = "measure"
    spec = dataset_charts(dataset)[0]
    assert spec["x"] == {"column": "dimension_a", "role": "dimension", "label": "维度甲", "grain": None}
    assert spec["categories"] == [102, 101]
    assert [(s["column"], s["label"], s["values"]) for s in spec["series"]] == [("value_a", "指标甲", [0.3, 0.2])]
    chart = export._spec_chart(spec, "Helvetica")
    labels = [item.text for item in chart.contents if isinstance(item, String)]
    assert "101" in labels and "102" in labels


def test_export_empty_and_partial_contracts_do_not_guess_numeric_columns():
    dataset = {
        "ref": "D1",
        "columns": ["dimension_a", "value_a"],
        "rows": [[101, 1], [102, 3]],
        "column_roles": {},
    }
    view = {"datasets": [dataset], "report": {"claims": [{"refs": ["D1"]}]}}
    for roles in ({}, {"dimension_a": {"role": "dimension", "label": "维度甲"}}):
        dataset["column_roles"] = roles
        assert dataset_charts(dataset) == []
        assert report_figures(view) == []
        assert report_figures(view, ["D1"]) == []
    dataset.pop("column_roles")
    # 旧运行也必须由元数据恢复契约，不再按单元格数值猜列。
    assert dataset_charts(dataset) == [] and report_figures(view) == []
    dataset["meta"] = {"units": {"value_a": {"kind": "count"}}}
    assert report_figures(view)[0]["chart"]["series"][0]["column"] == "value_a"


def test_export_contract_time_overrides_legacy_time_column_and_numeric_series_keys():
    dataset = {
        "columns": ["time_a", "dimension_a", "value_a"],
        "time_column": "dimension_a",
        "rows": [["2026-01", 101, 10], ["2026-02", 101, 20], ["2026-03", 101, 30]],
        "column_roles": {
            "time_a": {"role": "time", "label": "time_a"},
            "dimension_a": {"role": "dimension", "label": "维度甲"},
            "value_a": {"role": "measure", "label": "指标甲"},
        },
    }
    spec = dataset_charts(dataset)[0]
    assert spec["kind"] == "line" and spec["x"]["column"] == "time_a"
    assert spec["categories"] == ["2026-01", "2026-02", "2026-03"]
    assert [(s["column"], s["name"], s["values"]) for s in spec["series"]] == [("value_a", "101", [10.0, 20.0, 30.0])]
    chart = export._spec_chart(spec, "Helvetica")
    assert len([item for item in chart.contents if isinstance(item, Circle)]) == 3
    assert set(spec["categories"]) <= {item.text for item in chart.contents if isinstance(item, String)}
    dataset["meta"] = {"column_roles": dataset.pop("column_roles")}
    assert dataset_charts(dataset)[0] == spec


def test_export_change_axis_comes_from_contract_instead_of_unit_or_column_name():
    dataset = {
        "columns": ["dimension_a", "列·变化", "value_a"],
        "rows": [[101, 900, -7], [102, 800, 3]],
        "column_roles": {
            "dimension_a": {"role": "dimension", "label": "维度甲"},
            "列·变化": {"role": "measure", "label": "指标甲"},
            "value_a": {"role": "change", "label": "指标甲"},
        },
        "units": {"列·变化": {"kind": "delta"}, "value_a": {"kind": "level"}},
    }
    # 普通图不再一律优先 change；完整对比组的变化图由
    # test_charts.py::test_comparison_dimension_precedes_time_and_sorts_each_chart_independently 覆盖。
    spec = dataset_charts(dataset)[0]
    assert spec["x"]["column"] == "dimension_a"
    assert [(s["column"], s["values"]) for s in spec["series"]] == [("列·变化", [900.0, 800.0])]
    # 去掉 measure 契约后才使用 change，不能由单位或“·变化”后缀决定系列。
    del dataset["column_roles"]["列·变化"]
    spec = dataset_charts(dataset)[0]
    assert spec["categories"] == [102, 101]
    assert [(s["column"], s["values"]) for s in spec["series"]] == [("value_a", [3.0, -7.0])]
    chart = export._spec_chart(spec, "Helvetica")
    labels = {item.text for item in chart.contents if isinstance(item, String)}
    assert {"101", "102", "-7"} <= labels


@pytest.mark.parametrize("render", [export.to_markdown, export.to_html])
def test_export_member_cause_uses_dimension_label_in_report(render):
    cause = {
        "type": "member",
        "entity": {"dimension": "dimension_a", "dimension_label": "维度甲", "member": "成员甲"},
        "direction": "down",
        "contribution": {"text": "贡献待核实"},
    }
    view = {"report": {"title": "报告甲", "causes": [cause]}, "datasets": [], "verification": {}}
    assert "维度甲：成员甲" in render(view, audience="full")
    assert export._cause_label({}) == "未标注实体"
    assert export._cause_label({"type": "member"}) == "未标注实体"
    assert export._cause_label({"type": "calendar"}) == "日历因素"
    assert export._cause_label({"type": "member", "entity": {"member": "成员甲"}}) == "成员甲"


def _comparison_view(kind="level"):
    columns = ["指标甲", "指标甲·上一自然期", "指标甲·变化", "指标甲·变化率"]
    dataset = {
        "ref": "D1",
        "kind": "governed",
        "columns": columns,
        "rows": [[40, 50, -10, -0.2]],
        "meta": {
            "intent": {
                "compare": "previous_calendar",
                "start_date": "2026-03-01",
                "end_date": "2026-03-31",
                "compare_start": "2026-01-01",
                "compare_end": "2026-01-31",
            },
            "window": {"start": "2026-03-01", "end": "2026-03-31"},
            "units": {
                columns[0]: {"kind": kind},
                columns[1]: {"kind": kind},
                columns[2]: {"kind": "delta"},
                columns[3]: {"kind": "rate_change"},
            },
        },
    }
    return {"report": {"claims": [{"refs": ["D1"]}]}, "datasets": [dataset]}


def test_export_previous_calendar_header_and_core_scope_share_comparison_range():
    view = _comparison_view()
    scope = {"compare_start": "2026-02-01", "compare_end": "2026-02-28"}
    assert export._core_scope_matches(view["datasets"][0], scope)
    assert not export._core_scope_matches(view["datasets"][0], {"compare_start": "2026-01-29"})
    assert "本期 2026-03-01 ~ 03-31 · 对比 2026-02-01 ~ 02-28" in export._header(view)
    assert export.CALC_PERIOD_LABELS["previous_calendar"] == "上一自然期"


def test_export_count_quantity_keeps_core_metric_cards():
    cards = export._core_metrics(_comparison_view("count"))
    assert [card["measure"] for card in cards] == ["count", "count", "delta", "rate_change"]
    assert [card["raw"] for card in cards] == [Decimal(40), Decimal(50), Decimal(-10), Decimal("-0.2")]


@pytest.mark.parametrize("style", ["cn", "intl"])
def test_number_style_toml_environment_and_settings_view_share_config(style, tmp_path, monkeypatch):
    path = tmp_path / "avelis.toml"
    path.write_text(f'[business]\nnumber_style = "{style}"\n', encoding="utf-8")
    parsed = configfile.read_config({"AVELIS_CONFIG": str(path)})
    assert not parsed.problems and parsed["AVELIS_NUMBER_STYLE"] == style
    settings = config.build_settings(
        configfile.merge_layers(parsed, configfile.ConfigValues({"AVELIS_SECRET": SECRET}))
    )
    assert settings.number_style == style
    override = "intl" if style == "cn" else "cn"
    values = configfile.merge_layers(
        parsed, configfile.ConfigValues({"AVELIS_SECRET": SECRET, "AVELIS_NUMBER_STYLE": override})
    )
    assert config.build_settings(values).number_style == override
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth.modelcfg, "settings_view", lambda session: {})
    public = auth.settings_view(object(), object())
    assert public["number_style"] == style and public["business_timezone"] == settings.business_timezone


def test_number_style_defaults_to_cn_and_rejects_unknown_enum():
    assert config.Settings.number_style == configfile.CONFIG_KEYS["business.number_style"].default == "cn"
    assert config.build_settings({"AVELIS_SECRET": SECRET}).number_style == "cn"
    with pytest.raises(configfile.ConfigError, match="AVELIS_NUMBER_STYLE"):
        config.build_settings({"AVELIS_SECRET": SECRET, "AVELIS_NUMBER_STYLE": "unknown"})


@pytest.mark.parametrize(
    "style,value,expected",
    [
        ("cn", "9999", "9999单位甲"),
        ("intl", "1000", "1000单位甲"),
        ("intl", "9999", "9999单位甲"),
        ("cn", "10000", "1.00万单位甲"),
        ("intl", "10000", "10.00K单位甲"),
        ("cn", "1000000", "100.00万单位甲"),
        ("intl", "1000000", "1.00M单位甲"),
        ("cn", "100000000", "1.00亿单位甲"),
        ("intl", "100000000", "100.00M单位甲"),
        ("cn", "1000000000", "10.00亿单位甲"),
        ("intl", "1000000000", "1.00B单位甲"),
    ],
)
def test_number_style_thresholds_and_exports_reuse_grounding(monkeypatch, style, value, expected):
    monkeypatch.setattr(grounding, "get_settings", lambda: SimpleNamespace(number_style=style))
    unit = {"kind": "level", "unit": "单位甲"}
    assert grounding.display(Decimal(value), grounding._unit(unit)) == expected
    assert export._formatted(value, unit) == expected


def test_international_style_preserves_scale_sign_precision_and_ratio(monkeypatch):
    monkeypatch.setattr(grounding, "get_settings", lambda: SimpleNamespace(number_style="intl"))
    delta = grounding._unit({"kind": "delta", "unit": "单位甲", "scale": 1000000})
    assert grounding.display(Decimal("1.25"), delta) == "+1.25M单位甲"
    assert grounding.display(Decimal("-0.125"), delta, precision=1) == "-125.0K单位甲"
    assert grounding.display(Decimal("-0.125"), delta, direction="down") == "125.00K单位甲"
    assert grounding.display(Decimal("0.125"), grounding._unit({"kind": "ratio"})) == "12.5%"
    assert grounding.display(Decimal("0.125"), grounding._unit({"kind": "delta", "of_ratio": True})) == "+12.5个百分点"
    assert export._axis(9999) == "9999"
    assert export._axis(10000) == "10K"
    assert export._axis(1000000) == "1M"
    assert export._axis(1000000000) == "1B"
